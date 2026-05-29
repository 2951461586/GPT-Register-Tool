package main

import (
	"fmt"
	"log"
	"net"

	"github.com/byte-v-forge/gpt/gopay/appsvc"
	"github.com/byte-v-forge/gpt/gopay/pb"
	"google.golang.org/grpc"
)

func main() {
	cfg := appsvc.ConfigFromEnv()
	service, err := appsvc.NewServer(cfg)
	if err != nil {
		log.Fatalf("init gopay app service: %v", err)
	}
	listener, err := net.Listen("tcp", ":"+cfg.Port)
	if err != nil {
		log.Fatalf("listen gopay app service: %v", err)
	}
	server := grpc.NewServer()
	pb.RegisterGopayAppServiceServer(server, service)
	fmt.Printf("[gopay-app] Go gRPC listening on :%s\n", cfg.Port)
	if err := server.Serve(listener); err != nil {
		log.Fatalf("serve gopay app service: %v", err)
	}
}
