package main

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

func main() {
	n := 2000000
	if len(os.Args) > 1 {
		if v, err := strconv.Atoi(os.Args[1]); err == nil {
			n = v
		}
	}
	keep := make([][]byte, n)
	t0 := time.Now()
	for i := 0; i < n; i++ {
		keep[i] = make([]byte, 64)
	}
	d := time.Since(t0)
	ms := float64(d.Nanoseconds()) / 1e6
	fmt.Printf("go alloc n=%d ms=%.1f ns_per_op=%.1f\n", n, ms, ms*1e6/float64(n))
	keep = nil
}
