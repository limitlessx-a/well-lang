package main

import (
	"bufio"
	"crypto/sha1"
	"encoding/base64"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"strings"
)

const guid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

func frame(op byte, payload []byte) []byte {
	n := len(payload)
	h := []byte{0x80 | op, 0}
	if n < 126 {
		h[1] = byte(n)
	} else if n < 65536 {
		h[1] = 126
		h = append(h, byte(n>>8), byte(n))
	} else {
		h[1] = 127
		for i := 7; i >= 0; i-- {
			h = append(h, byte(uint64(n)>>(8*uint(i))))
		}
	}
	return append(h, payload...)
}

func handle(c net.Conn) {
	defer c.Close()
	br := bufio.NewReader(c)
	key := ""
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			return
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			break
		}
		if strings.HasPrefix(strings.ToLower(line), "sec-websocket-key:") {
			key = strings.TrimSpace(line[len("sec-websocket-key:"):])
		}
	}
	if key == "" {
		return
	}
	sum := sha1.Sum([]byte(key + guid))
	accept := base64.StdEncoding.EncodeToString(sum[:])
	fmt.Fprintf(c, "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n", accept)
	c.Write(frame(1, []byte("ready")))
	for {
		var h [2]byte
		if _, err := io.ReadFull(br, h[:]); err != nil {
			return
		}
		op := h[0] & 0x0f
		masked := h[1]&0x80 != 0
		n := int(h[1] & 0x7f)
		if n == 126 {
			var ext [2]byte
			if _, err := io.ReadFull(br, ext[:]); err != nil {
				return
			}
			n = int(ext[0])<<8 | int(ext[1])
		} else if n == 127 {
			var ext [8]byte
			if _, err := io.ReadFull(br, ext[:]); err != nil {
				return
			}
			n = 0
			for _, b := range ext {
				n = n<<8 | int(b)
			}
		}
		var mask [4]byte
		if masked {
			if _, err := io.ReadFull(br, mask[:]); err != nil {
				return
			}
		}
		payload := make([]byte, n)
		if _, err := io.ReadFull(br, payload); err != nil {
			return
		}
		if masked {
			for i := range payload {
				payload[i] ^= mask[i&3]
			}
		}
		if op == 8 {
			return
		}
		if op == 9 { // ping -> pong
			c.Write(frame(10, payload))
			continue
		}
		c.Write(frame(1, payload))
	}
}

func main() {
	port := 18100
	if len(os.Args) > 1 {
		if v, err := strconv.Atoi(os.Args[1]); err == nil {
			port = v
		}
	}
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		panic(err)
	}
	for {
		c, err := ln.Accept()
		if err != nil {
			continue
		}
		go handle(c)
	}
}
