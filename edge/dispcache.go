package main

// dispCache periodically snapshots Dispatcharr's XMLTV guide into a local gzip
// file and serves it instantly.
//
// Some Dispatcharr installations generate large XMLTV responses on demand and
// exceed player timeouts. The edge refreshes that guide off the client path,
// retains the last valid snapshot, and serves it from /output/epg.

import (
	"compress/gzip"
	"crypto/sha1"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"
)

type dispCache struct {
	url      string
	gzPath   string
	interval time.Duration
	client   *http.Client

	mu    sync.RWMutex
	ready bool
	mtime time.Time
	size  int64
	etag  string
}

func newDispCache(url, cacheDir string, interval time.Duration) *dispCache {
	return &dispCache{
		url:      url,
		gzPath:   filepath.Join(cacheDir, "dispatcharr.xml.gz"),
		interval: interval,
		// Generous timeout: a cold Dispatcharr regeneration is ~94s.
		client: &http.Client{Timeout: 5 * time.Minute},
	}
}

func (d *dispCache) start() {
	if d.url == "" {
		log.Printf("dispCache: DISPATCHARR_EPG_URL unset — /output/epg returns 503")
		return
	}
	if err := os.MkdirAll(filepath.Dir(d.gzPath), 0o755); err != nil {
		log.Printf("dispCache: mkdir %s: %v", filepath.Dir(d.gzPath), err)
	}
	// Adopt any snapshot left from a previous run so a restart serves
	// immediately instead of going dark for one refresh cycle.
	d.loadMeta()
	go func() {
		// Retry quickly on failure and keep the last valid snapshot. refresh()
		// only renames a complete HTTP 200 response into place.
		const retryDelay = 90 * time.Second
		for {
			if err := d.refresh(); err != nil {
				log.Printf("dispCache: refresh failed: %v (retry in %s)", err, retryDelay)
				time.Sleep(retryDelay)
				continue
			}
			time.Sleep(d.interval)
		}
	}()
	// Never log the configured URL: Dispatcharr endpoints commonly include XC
	// credentials in their query string.
	log.Printf("dispCache: started, source=configured gz=%s every=%s", d.gzPath, d.interval)
}

func (d *dispCache) refresh() error {
	start := time.Now()
	// Let Go's transport add Accept-Encoding and transparently decompress, so
	// resp.Body is always plain XML regardless of what Dispatcharr sends.
	resp, err := d.client.Get(d.url)
	if err != nil {
		var urlErr *url.Error
		if errors.As(err, &urlErr) {
			return fmt.Errorf("upstream request failed: %w", urlErr.Err)
		}
		return fmt.Errorf("upstream request failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, resp.Body)
		return fmt.Errorf("upstream status %d", resp.StatusCode)
	}

	tmp := d.gzPath + ".tmp"
	f, err := os.Create(tmp)
	if err != nil {
		return err
	}
	gz := gzip.NewWriter(f)
	n, copyErr := io.Copy(gz, resp.Body)
	closeErr := gz.Close()
	if cerr := f.Close(); closeErr == nil {
		closeErr = cerr
	}
	if copyErr != nil {
		os.Remove(tmp)
		return copyErr
	}
	if closeErr != nil {
		os.Remove(tmp)
		return closeErr
	}
	// Guard against overwriting a good snapshot with garbage (e.g. a 502 page
	// or a truncated body): the real guide is tens of MB of raw XML.
	if n < 1_000_000 {
		os.Remove(tmp)
		return fmt.Errorf("snapshot too small (%d raw bytes) — refusing to replace", n)
	}
	if err := os.Rename(tmp, d.gzPath); err != nil {
		os.Remove(tmp)
		return err
	}
	d.loadMeta()
	log.Printf("dispCache: refreshed (%d raw bytes) in %s", n, time.Since(start).Round(time.Second))
	return nil
}

func (d *dispCache) loadMeta() {
	st, err := os.Stat(d.gzPath)
	if err != nil {
		return
	}
	h := sha1.New()
	fmt.Fprintf(h, "disp|%d|%d", st.ModTime().UnixNano(), st.Size())
	d.mu.Lock()
	d.ready = true
	d.mtime = st.ModTime()
	d.size = st.Size()
	d.etag = `"` + hex.EncodeToString(h.Sum(nil))[:16] + `"`
	d.mu.Unlock()
}

// serve writes the cached guide with content negotiation: gzip transport
// encoding for clients that accept it (the file is already gzipped, so this is
// a straight copy), or decompressed XML for those that don't. Returns false if
// no snapshot exists yet so the caller can fall back.
func (d *dispCache) serve(w http.ResponseWriter, r *http.Request) bool {
	d.mu.RLock()
	ready, mtime, size, etag := d.ready, d.mtime, d.size, d.etag
	d.mu.RUnlock()
	if !ready {
		return false
	}

	w.Header().Set("Cache-Control", cacheControl)
	w.Header().Set("ETag", etag)
	w.Header().Set("Last-Modified", mtime.UTC().Format(http.TimeFormat))
	w.Header().Set("Vary", "Accept-Encoding")
	if inm := r.Header.Get("If-None-Match"); inm != "" && inm == etag {
		w.WriteHeader(http.StatusNotModified)
		return true
	}
	w.Header().Set("Content-Type", "application/xml")
	w.Header().Set("Content-Disposition", `inline; filename="epg.xml"`)

	f, err := os.Open(d.gzPath)
	if err != nil {
		return false
	}
	defer f.Close()

	if clientAcceptsGzip(r) {
		// File is already gzip — hand it over as transport-encoded XML.
		w.Header().Set("Content-Encoding", "gzip")
		w.Header().Set("Content-Length", strconv.FormatInt(size, 10))
		if r.Method == http.MethodHead {
			return true
		}
		io.Copy(w, f)
		return true
	}

	// Client can't take gzip — decompress on the fly (length unknown).
	if r.Method == http.MethodHead {
		return true
	}
	gr, err := gzip.NewReader(f)
	if err != nil {
		return false
	}
	defer gr.Close()
	io.Copy(w, gr)
	return true
}
