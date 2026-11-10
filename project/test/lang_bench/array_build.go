package main

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

var last []int64

func main() {
	n := 2000000
	if len(os.Args) > 1 {
		if v, err := strconv.Atoi(os.Args[1]); err == nil { n = v }
	}
	var sink int64
	t0 := time.Now()
	for i := 0; i < n; i++ {
		a := make([]int64, 0, 2)
		a = append(a, 1, 2)
		sink += a[0] + a[1]
		last = a
	}
	d := time.Since(t0)
	ms := float64(d.Nanoseconds()) / 1e6
	fmt.Printf("go-arrbuild n=%d ms=%.1f ns_per_op=%.1f sink=%d\n", n, ms, ms*1e6/float64(n), sink)
}
