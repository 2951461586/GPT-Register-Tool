package app

import "os"

func getenv(name string) string {
	return os.Getenv(name)
}
