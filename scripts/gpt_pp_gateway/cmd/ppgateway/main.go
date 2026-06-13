package main

import (
	"flag"
	"log"
	"net/http"
	"os"
	"time"

	"pp-longlink/internal/gateway"
)

func main() {
	addr := flag.String("addr", ":8787", "listen address")
	staticDir := flag.String("static", "webapp/static", "static directory")
	timeoutSec := flag.Int("timeout", 45, "extraction timeout in seconds")
	flag.Parse()

	cfg := gateway.DefaultConfig()
	cfg.Timeout = time.Duration(*timeoutSec) * time.Second
	cfg.MaxAttempts = 1
	cfg.CheckoutParallel = 1
	cfg.RaceParallel = 1
	cfg.ProxyRotations = 4
	cfg.Country = "US"
	cfg.Currency = "USD"
	cfg.AllowNonZero = true
	extractor := gateway.NewExtractor(cfg)
	srv := gateway.NewServer(extractor)
	mux := http.NewServeMux()
	srv.Register(mux)
	if st, err := os.Stat(*staticDir); err == nil && st.IsDir() {
		static := http.FileServer(http.Dir(*staticDir))
		mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
			w.Header().Set("Pragma", "no-cache")
			static.ServeHTTP(w, r)
		})
	}
	log.Printf("pp gateway go runtime listening on %s", *addr)
	log.Fatal(http.ListenAndServe(*addr, mux))
}
