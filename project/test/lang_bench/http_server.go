package main

import (
	"fmt"
	"net/http"
	"os"
	"strconv"
)

func main() {
	port := 18080
	if len(os.Args) > 1 {
		if v, err := strconv.Atoi(os.Args[1]); err == nil {
			port = v
		}
	}
	http.HandleFunc("/bench", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.Header().Set("Content-Length", "2")
		w.WriteHeader(200)
		_, _ = w.Write([]byte("ok"))
	})
	srv := &http.Server{Addr: fmt.Sprintf("127.0.0.1:%d", port)}
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		panic(err)
	}
}
