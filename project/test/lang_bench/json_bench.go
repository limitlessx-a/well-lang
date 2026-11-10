package main

import (
	"encoding/json"
	"fmt"
	"time"
)

type Addr struct {
	City string `json:"city"`
	Zip  int    `json:"zip"`
}

type User struct {
	ID   int64    `json:"id"`
	Name string   `json:"name"`
	Age  int      `json:"age"`
	Tags []string `json:"tags"`
	Addr Addr     `json:"addr"`
}

func main() {
	text := []byte(`{"id":123,"name":"alice","age":30,"tags":["a","b","c"],"addr":{"city":"NY","zip":10001}}`)
	const iters = 300000
	var acc int64
	for i := 0; i < 2000; i++ {
		var u User
		_ = json.Unmarshal(text, &u)
		acc += u.ID
	}
	t0 := time.Now()
	for i := 0; i < iters; i++ {
		var u User
		if err := json.Unmarshal(text, &u); err != nil {
			panic(err)
		}
		acc += u.ID + int64(len(u.Name)) + int64(len(u.Tags)) + int64(u.Addr.Zip)
	}
	t1 := time.Now()
	var one User
	_ = json.Unmarshal(text, &one)
	for i := 0; i < iters; i++ {
		b, err := json.Marshal(&one)
		if err != nil {
			panic(err)
		}
		acc += int64(len(b))
	}
	t2 := time.Now()
	fmt.Printf("parse_ns %.0f\nstringify_ns %.0f\nchecksum %d\n",
		float64(t1.Sub(t0).Nanoseconds())/iters,
		float64(t2.Sub(t1).Nanoseconds())/iters, acc)
}
