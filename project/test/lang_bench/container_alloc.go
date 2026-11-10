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
		if v, err := strconv.Atoi(os.Args[1]); err == nil {
			n = v
		}
	}
	var sink int64
	t0 := time.Now()
	for i := 0; i < n; i++ {
		s := make([]int64, 0, 1)
		s = append(s, int64(i))
		sink += int64(len(s))
		last = s
	}
	d := time.Since(t0)
	ms := float64(d.Nanoseconds()) / 1e6
	fmt.Printf("go-slice n=%d ms=%.1f ns_per_op=%.1f sink=%d\n",
		n, ms, ms*1e6/float64(n), sink)
}
