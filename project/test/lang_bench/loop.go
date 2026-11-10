package main

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

func main() {
	n := int64(100000000)
	if len(os.Args) > 1 {
		if v, err := strconv.ParseInt(os.Args[1], 10, 64); err == nil {
			n = v
		}
	}
	var acc int64
	t0 := time.Now()
	for i := int64(0); i < n; i++ {
		acc += i
	}
	d := time.Since(t0)
	ms := float64(d.Nanoseconds()) / 1e6
	fmt.Printf("go n=%d ms=%.1f ns_per_iter=%.3f acc=%d\n",
		n, ms, ms*1e6/float64(n), acc)
}
