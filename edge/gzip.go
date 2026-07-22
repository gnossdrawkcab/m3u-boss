package main

import (
	"compress/gzip"
	"io"
)

// Thin wrapper so main.go can pretend gzip.NewWriter is a tiny helper.
func newGzipWriter(w io.Writer) *gzip.Writer { return gzip.NewWriter(w) }
