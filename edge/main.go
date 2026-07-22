// m3u-edge — front door for the m3u-boss app.
//
// Owns the polling hot path: /m3u, /epg (and aliases /epg.xml, /m3u.gz,
// /epg.gz, /epg.xml.gz). Reads directly from disk with proper HTTP cache semantics
// (ETag, Last-Modified, 304 Not Modified, HEAD, gzip negotiation) so that
// IPTV clients hammering this URL no longer wake the Python backend at all.
//
// Direct M3U/XMLTV feed paths require the independent M3U_BOSS_FEED_TOKEN.
// Teamarr routes validate their own credentials in the Python application.
// Everything else (the management UI and its destructive CRUD API) is reverse-
// proxied to the Python backend ONLY when the request carries Cloudflare
// Access's authenticated-user header, so the unauthenticated admin surface is
// no longer exposed to the bare LAN port. See loadConfig / isPublicPath.
package main

import (
	"crypto/sha1"
	"crypto/subtle"
	"encoding/hex"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ── Config ──────────────────────────────────────────────────────────────────

type config struct {
	listenAddr      string
	backendURL      *url.URL
	exportsDir      string
	requireCFAccess bool
	dispEPGURL      string        // Dispatcharr xmltv.php (with XC creds) to snapshot
	cacheDir        string        // where the gzipped EPG snapshot is stored
	dispRefresh     time.Duration // how often to re-snapshot Dispatcharr's guide
	feedToken       string        // query token required by direct M3U/XMLTV feeds
}

func loadConfig() *config {
	listen := getenv("LISTEN_ADDR", ":43817")
	backend := getenv("BACKEND_URL", "http://m3u-boss:43818")
	exports := getenv("EXPORTS_DIR", "/app/exports")
	dispURL := getenv("DISPATCHARR_EPG_URL", "")
	cacheDir := getenv("CACHE_DIR", "/app/cache")
	refreshMin := 30
	if v := os.Getenv("DISP_EPG_REFRESH_MIN"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			refreshMin = n
		}
	}
	u, err := url.Parse(backend)
	if err != nil {
		log.Fatalf("invalid BACKEND_URL %q: %v", backend, err)
	}
	// When true (default), only the read-only feed/asset allowlist is reachable
	// without auth. Everything else (the management UI + destructive CRUD API)
	// requires the Cf-Access-Authenticated-User-Email header that Cloudflare
	// Access injects — i.e. it's only reachable through the gated tunnel, not
	// from a direct LAN hit. Set UI_REQUIRE_CF_ACCESS=false to fall back to the
	// old "proxy everything" behavior (e.g. emergency local access).
	requireCF := getenv("UI_REQUIRE_CF_ACCESS", "true") != "false"
	return &config{
		listenAddr:      listen,
		backendURL:      u,
		exportsDir:      exports,
		requireCFAccess: requireCF,
		dispEPGURL:      dispURL,
		cacheDir:        cacheDir,
		dispRefresh:     time.Duration(refreshMin) * time.Minute,
		feedToken:       os.Getenv("M3U_BOSS_FEED_TOKEN"),
	}
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// ── Cached file metadata ────────────────────────────────────────────────────

// staticEntry describes one of the four export files the edge serves. We keep
// a tiny mtime+size cache so that the common "polling 304" path doesn't touch
// the disk past a single stat() call.
type staticEntry struct {
	rawName string // organized.m3u or organized.xml
	gzName  string // organized.m3u.gz or organized.xml.gz
	media   string // base media-type (we still serve octet-stream on download=1)
	mtime   time.Time
	size    int64
	etag    string
}

func (c *config) stat(name string) (time.Time, int64, error) {
	st, err := os.Stat(filepath.Join(c.exportsDir, name))
	if err != nil {
		return time.Time{}, 0, err
	}
	return st.ModTime(), st.Size(), nil
}

func (c *config) loadMeta(e *staticEntry) error {
	mt, sz, err := c.stat(e.rawName)
	if err != nil {
		return err
	}
	if mt.Equal(e.mtime) && sz == e.size && e.etag != "" {
		return nil
	}
	e.mtime = mt
	e.size = sz
	// ETag = sha1(rawName|mtime|size). Stable as long as the file hasn't
	// changed, cheap to compute (a few bytes hashed once per change).
	h := sha1.New()
	fmt.Fprintf(h, "%s|%d|%d", e.rawName, mt.UnixNano(), sz)
	e.etag = `"` + hex.EncodeToString(h.Sum(nil))[:16] + `"`
	return nil
}

// ── Conditional GET helpers ─────────────────────────────────────────────────

const cacheControl = "public, max-age=30, must-revalidate"

func notModified(r *http.Request, e *staticEntry) bool {
	if inm := r.Header.Get("If-None-Match"); inm != "" {
		// Allow comma-separated list per RFC.
		for _, tag := range strings.Split(inm, ",") {
			if strings.TrimSpace(tag) == e.etag || strings.TrimSpace(tag) == "*" {
				return true
			}
		}
	}
	if ims := r.Header.Get("If-Modified-Since"); ims != "" {
		if t, err := http.ParseTime(ims); err == nil {
			if !e.mtime.Truncate(time.Second).After(t.Truncate(time.Second)) {
				return true
			}
		}
	}
	return false
}

func writeCacheHeaders(w http.ResponseWriter, e *staticEntry) {
	w.Header().Set("Cache-Control", cacheControl)
	w.Header().Set("ETag", e.etag)
	w.Header().Set("Last-Modified", e.mtime.UTC().Format(http.TimeFormat))
	w.Header().Set("Vary", "Accept-Encoding")
}

func clientAcceptsGzip(r *http.Request) bool {
	ae := strings.ToLower(r.Header.Get("Accept-Encoding"))
	return strings.Contains(ae, "gzip")
}

// ── Handlers ────────────────────────────────────────────────────────────────

// serveStatic is the workhorse: serves an export file with full HTTP caching.
// If viewQ is set the response is text/plain (browser inline view); if
// downloadQ is set it's application/octet-stream attachment. Otherwise it's
// the IPTV-client default (audio/x-mpegurl or application/xml) and we'll
// prefer the pre-gzipped .gz file if the client supports gzip.
func (s *server) serveStatic(w http.ResponseWriter, r *http.Request, e *staticEntry) {
	if err := s.cfg.loadMeta(e); err != nil {
		log.Printf("loadMeta(%s) failed: %v (dir=%s)", e.rawName, err, s.cfg.exportsDir)
		http.Error(w, "export not ready: "+err.Error(), http.StatusServiceUnavailable)
		return
	}

	if notModified(r, e) {
		writeCacheHeaders(w, e)
		w.WriteHeader(http.StatusNotModified)
		return
	}

	q := r.URL.Query()
	view := q.Get("view") == "1" || q.Get("view") == "true"
	download := q.Get("download") == "1" || q.Get("download") == "true"

	writeCacheHeaders(w, e)
	w.Header().Set("Content-Disposition", `inline; filename="`+e.rawName+`"`)

	// For /m3u we always need to patch the #EXTM3U header with the request's
	// base URL so IPTV apps fetch the EPG from whatever hostname the user is
	// hitting (LAN IP / domain / tunnel). The body is small (~3MB) so we
	// just read it into memory once.
	if e.rawName == "organized.m3u" {
		s.serveM3U(w, r, e, view, download)
		return
	}

	// For /epg, serve the pre-gzipped file when the client supports gzip
	// AND we're not in view/download mode. 33MB on the wire vs 111MB raw.
	if !view && !download && clientAcceptsGzip(r) {
		gzPath := filepath.Join(s.cfg.exportsDir, e.gzName)
		if st, err := os.Stat(gzPath); err == nil {
			f, err := os.Open(gzPath)
			if err == nil {
				defer f.Close()
				w.Header().Set("Content-Encoding", "gzip")
				w.Header().Set("Content-Length", strconv.FormatInt(st.Size(), 10))
				w.Header().Set("Content-Type", e.media)
				if r.Method == http.MethodHead {
					return
				}
				io.Copy(w, f)
				return
			}
		}
	}

	// Plain file send — use http.ServeFile which handles range/HEAD efficiently.
	media := e.media
	if view {
		media = "text/plain; charset=utf-8"
	} else if download {
		media = "application/octet-stream"
		w.Header().Set("Content-Disposition", `attachment; filename="`+e.rawName+`"`)
	}
	w.Header().Set("Content-Type", media)
	w.Header().Set("Content-Length", strconv.FormatInt(e.size, 10))
	if r.Method == http.MethodHead {
		return
	}
	f, err := os.Open(filepath.Join(s.cfg.exportsDir, e.rawName))
	if err != nil {
		http.Error(w, "export not ready", http.StatusServiceUnavailable)
		return
	}
	defer f.Close()
	io.Copy(w, f)
}

// serveM3U patches the #EXTM3U header with the request's base URL so the
// url-tvg attribute points at the right host. Body is read into memory once
// (~3MB); patched body is cached by (base, mtime) across requests.
type m3uCacheEntry struct {
	base  string
	mtime time.Time
	bytes []byte
}

var m3uCache = struct {
	mu    sync.RWMutex
	entry *m3uCacheEntry
}{}

func (s *server) serveM3U(w http.ResponseWriter, r *http.Request, e *staticEntry, view, download bool) {
	base := requestBase(r)

	// Fast-path: do we already have this base + mtime cached?
	m3uCache.mu.RLock()
	cur := m3uCache.entry
	m3uCache.mu.RUnlock()
	if cur != nil && cur.base == base && cur.mtime.Equal(e.mtime) {
		s.writeM3UBody(w, r, e, cur.bytes, view, download)
		return
	}

	// Read the file, patch the first line.
	raw, err := os.ReadFile(filepath.Join(s.cfg.exportsDir, e.rawName))
	if err != nil {
		http.Error(w, "m3u not ready", http.StatusServiceUnavailable)
		return
	}
	// Use the EPG file's mtime as the cache-busting version on the url-tvg URL.
	epgMtime, _, _ := s.cfg.stat("organized.xml")
	epgVer := epgMtime.Unix()
	if epgVer == 0 {
		epgVer = e.mtime.Unix()
	}
	// Use /epg.gz — TiviMate (and similar clients) don't send Accept-Encoding:gzip
	// so /epg would serve the full 93MB XML. The .gz endpoint always returns the
	// 17MB pre-compressed file; IPTV apps handle .gz URLs natively.
	epgURL := fmt.Sprintf("%s/epg.gz?v=%d&token=%s", base, epgVer,
		url.QueryEscape(s.cfg.feedToken))
	attrs := fmt.Sprintf(`url-tvg="%s" x-tvg-url="%s"`, epgURL, epgURL)

	patched := raw
	if idx := strings.Index(string(raw[:min(len(raw), 256)]), "\n"); idx >= 0 {
		firstLine := string(raw[:idx])
		if strings.HasPrefix(firstLine, "#EXTM3U") {
			newFirst := "#EXTM3U " + attrs + firstLine[len("#EXTM3U"):]
			patched = append([]byte(newFirst+"\n"), raw[idx+1:]...)
		}
	}

	m3uCache.mu.Lock()
	m3uCache.entry = &m3uCacheEntry{base: base, mtime: e.mtime, bytes: patched}
	m3uCache.mu.Unlock()
	s.writeM3UBody(w, r, e, patched, view, download)
}

func (s *server) writeM3UBody(w http.ResponseWriter, r *http.Request, e *staticEntry, body []byte, view, download bool) {
	media := e.media
	if view {
		media = "text/plain; charset=utf-8"
	} else if download {
		media = "application/octet-stream"
		w.Header().Set("Content-Disposition", `attachment; filename="organized.m3u"`)
	}
	w.Header().Set("Content-Type", media)
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	if r.Method == http.MethodHead {
		return
	}
	// gzip-on-the-fly if requested — body is small (~3MB) so this is cheap.
	if clientAcceptsGzip(r) && len(body) > 4096 && !view && !download {
		w.Header().Set("Content-Encoding", "gzip")
		w.Header().Del("Content-Length")
		gz := newGzipWriter(w)
		defer gz.Close()
		gz.Write(body)
		return
	}
	w.Write(body)
}

func requestBase(r *http.Request) string {
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	if proto := r.Header.Get("X-Forwarded-Proto"); proto != "" {
		scheme = proto
	}
	host := r.Host
	if h := r.Header.Get("X-Forwarded-Host"); h != "" {
		host = h
	}
	return scheme + "://" + host
}

// ── Server ──────────────────────────────────────────────────────────────────

type server struct {
	cfg    *config
	proxy  *httputil.ReverseProxy
	m3uEnt *staticEntry
	epgEnt *staticEntry
	disp   *dispCache
}

func newServer(cfg *config) *server {
	rp := httputil.NewSingleHostReverseProxy(cfg.backendURL)
	rp.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		log.Printf("backend error %s %s: %v", r.Method, r.URL.Path, err)
		http.Error(w, "backend unavailable", http.StatusBadGateway)
	}
	s := &server{
		cfg:    cfg,
		proxy:  rp,
		m3uEnt: &staticEntry{rawName: "organized.m3u", gzName: "organized.m3u.gz", media: "audio/x-mpegurl"},
		epgEnt: &staticEntry{rawName: "organized.xml", gzName: "organized.xml.gz", media: "application/xml"},
		disp:   newDispCache(cfg.dispEPGURL, cfg.cacheDir, cfg.dispRefresh),
	}
	s.disp.start()
	return s
}

func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Intercept the polling hot path and serve straight from disk.
	switch r.URL.Path {
	case "/m3u", "/api/export/m3u":
		if !s.feedAuthorized(r) {
			http.Error(w, "invalid feed token", http.StatusUnauthorized)
			return
		}
		s.serveStatic(w, r, s.m3uEnt)
		return
	case "/epg", "/epg.xml", "/api/export/epg":
		if !s.feedAuthorized(r) {
			http.Error(w, "invalid feed token", http.StatusUnauthorized)
			return
		}
		s.serveStatic(w, r, s.epgEnt)
		return
	case "/epg.gz", "/epg.xml.gz", "/api/export/epg.gz":
		if !s.feedAuthorized(r) {
			http.Error(w, "invalid feed token", http.StatusUnauthorized)
			return
		}
		s.serveEPGGzip(w, r)
		return
	case "/output/epg":
		if !s.feedAuthorized(r) {
			http.Error(w, "invalid feed token", http.StatusUnauthorized)
			return
		}
		// Player-facing EPG (XC + Dispatcharr's alias). Serve the cached
		// snapshot of Dispatcharr's correctly-mapped guide so clients get an
		// instant download instead of waiting on Dispatcharr's ~94s live
		// generation. Until the first snapshot lands, tell the client to retry.
		if s.disp != nil && s.disp.serve(w, r) {
			return
		}
		w.Header().Set("Retry-After", "60")
		http.Error(w, "EPG snapshot warming up — retry shortly", http.StatusServiceUnavailable)
		return
	}

	// Everything else is proxied to Python. Read-only feed/asset paths stay
	// open to the LAN; the management UI + CRUD API require the Cloudflare
	// Access header (present only on requests that came through the gated
	// tunnel). A direct LAN hit to, say, /api/sources/reset now 404s.
	if s.cfg.requireCFAccess && !isPublicPath(r.URL.Path) &&
		r.Header.Get("Cf-Access-Authenticated-User-Email") == "" {
		http.NotFound(w, r)
		return
	}
	s.proxy.ServeHTTP(w, r)
}

func (s *server) feedAuthorized(r *http.Request) bool {
	want := []byte(s.cfg.feedToken)
	got := []byte(r.URL.Query().Get("token"))
	return len(want) > 0 && len(want) == len(got) && subtle.ConstantTimeCompare(want, got) == 1
}

// isPublicPath reports whether a path is part of the read-only feed surface
// that LAN clients legitimately need without authentication: the alternate EPG
// outputs, the match-logo composites referenced by EPG programme icons, and the
// edge's HLS stream proxy. The destructive CRUD API lives elsewhere under /api
// and is intentionally NOT matched here.
// serveEPGGzip always serves the pre-gzipped EPG as a raw .gz file.
// IPTV clients (TiviMate, etc.) that don't request Accept-Encoding: gzip can
// still download the 17MB file and decompress it themselves when the URL ends
// in .gz — beats forcing them to download the 93MB raw XML.
func (s *server) serveEPGGzip(w http.ResponseWriter, r *http.Request) {
	gzPath := filepath.Join(s.cfg.exportsDir, "organized.xml.gz")
	st, err := os.Stat(gzPath)
	if err != nil {
		http.Error(w, "export not ready", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/gzip")
	w.Header().Set("Content-Disposition", `inline; filename="organized.xml.gz"`)
	w.Header().Set("Content-Length", strconv.FormatInt(st.Size(), 10))
	w.Header().Set("Cache-Control", cacheControl)
	if r.Method == http.MethodHead {
		return
	}
	f, err := os.Open(gzPath)
	if err != nil {
		http.Error(w, "export not ready", http.StatusServiceUnavailable)
		return
	}
	defer f.Close()
	io.Copy(w, f)
}

func isPublicPath(p string) bool {
	switch p {
	case "/healthz", "/xmltv.php", "/teamarr.xml":
		return true
	}
	return strings.HasPrefix(p, "/logos/") || strings.HasPrefix(p, "/api/stream/")
}

func main() {
	cfg := loadConfig()
	srv := newServer(cfg)

	// Trim verbose request logging; FastAPI / IPTV clients hammer this and we
	// don't need every 304 in syslog.
	httpSrv := &http.Server{
		Addr:              cfg.listenAddr,
		Handler:           srv,
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	log.Printf("m3u-edge listening on %s, backend=%s, exports=%s",
		cfg.listenAddr, cfg.backendURL, cfg.exportsDir)
	if err := httpSrv.ListenAndServe(); err != nil {
		log.Fatal(err)
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
