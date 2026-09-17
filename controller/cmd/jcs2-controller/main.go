//go:build windows

package main

import "os"

// The Windows polling/XInput implementation is intentionally kept behind the
// small event protocol in controller.Events. The runtime adapter consumes this
// stdout stream and owns the selected emulator console/ADB transport.
func main() { os.Exit(run()) }
