// A small TCP liveness probe that does not preload payment's Node instrumentation.
package main

import (
	"net"
	"os"
	"time"
)

func main() {
	connection, err := net.DialTimeout("tcp", "127.0.0.1:50051", 2*time.Second)
	if err != nil {
		os.Exit(1)
	}
	connection.Close()
}
