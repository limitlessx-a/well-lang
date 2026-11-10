package main

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

func main() {
	n := 20000000
	if len(os.Args) > 1 {
		if v, err := strconv.Atoi(os.Args[1]); err == nil {
			n = v
		}
	}
	a := make([]int64, 1024)
	for i := range a {
		a[i] = int64(i)
	}
	var acc int64
	t0 := time.Now()
	for i := 0; i < n; i++ {
		acc += a[i&1023]
	}
	d := time.Since(t0)
	ms := float64(d.Nanoseconds()) / 1e6
	fmt.Printf("go-arr n=%d ms=%.1f ns_per_iter=%.3f acc=%d\n",
		n, ms, ms*1e6/float64(n), acc)
}
