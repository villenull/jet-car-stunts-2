//go:build !windows

package main

import (
	"flag"
	"fmt"
	"jcs2/controller"
	"os"
)

func main() {
	port := flag.Int("console-port", 5596, "emulator console TCP port")
	tokenPath := flag.String("token-file", "", "dedicated emulator console auth token file")
	replayPath := flag.String("replay", "", "NDJSON replay file (use - for stdin)")
	mappingPath := flag.String("mapping", "mapping.json", "mapping JSON")
	trace := flag.Bool("trace", false, "also write mapped NDJSON to stdout")
	live := flag.Bool("live", false, "coalesce pending live axis samples; preserve button edges")
	adbPath := flag.String("adb-path", "adb", "explicit adb executable for helper transport")
	adbPort := flag.String("adb-port", "5038", "dedicated adb server port")
	serial := flag.String("adb-serial", "127.0.0.1:5595", "explicit emulator serial")
	helperJar := flag.String("helper-jar", "", "persistent app_process helper jar; enables UHID transport")
	readyFile := flag.String("ready-file", "", "absolute host JSON readiness marker")
	flag.Parse()
	if *replayPath == "" || (*tokenPath == "" && *helperJar == "") {
		fmt.Fprintln(os.Stderr, "--replay and either --token-file or --helper-jar are required")
		os.Exit(2)
	}
	m, err := controller.LoadMapping(*mappingPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	var sink controller.EventSink
	var closeSink func()
	if *helperJar != "" {
		h, err := controller.StartHelper(*adbPath, *adbPort, *serial, *helperJar)
		if err != nil {
			fmt.Fprintln(os.Stderr, "helper:", err)
			os.Exit(1)
		}
		if err := controller.WriteReadyFile(*readyFile, *serial, h); err != nil {
			fmt.Fprintln(os.Stderr, "ready-file:", err)
			_ = h.Close()
			os.Exit(1)
		}
		sink = h
		closeSink = func() { _ = h.Close() }
	} else {
		c, conn, err := controller.ConnectConsole(fmt.Sprintf("127.0.0.1:%d", *port), *tokenPath)
		if err != nil {
			fmt.Fprintln(os.Stderr, "console:", err)
			os.Exit(1)
		}
		sink = c
		closeSink = func() { _ = conn.Close() }
	}
	defer func() {
		closeSink()
		if *readyFile != "" {
			_ = os.Remove(*readyFile)
		}
	}()
	var in *os.File
	if *replayPath == "-" {
		in = os.Stdin
	} else {
		in, err = os.Open(*replayPath)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		defer in.Close()
	}
	var out *os.File
	if *trace {
		out = os.Stdout
	}
	if *live {
		err = controller.ReplayLive(in, sink, m, out)
	} else {
		err = controller.Replay(in, sink, m, out)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		closeSink()
		if *readyFile != "" {
			_ = os.Remove(*readyFile)
		}
		os.Exit(1)
	}
}
