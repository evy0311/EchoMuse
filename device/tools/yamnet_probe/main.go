// yamnet_probe validates the exact runtime, model and class manifest used by
// firmware, then runs it at the real 480ms cadence. It does not open the mic
// or alter the running server and is intended to be run on one test Echo before
// sound detection is enabled there.
package main

import (
	"encoding/binary"
	"flag"
	"fmt"
	"math"
	"os"
	"sort"
	"syscall"
	"time"

	"github.com/wilbowes/EchoMuse/internal/soundevent"
	"github.com/wilbowes/EchoMuse/internal/wakeword/ort"
)

func main() {
	lib := flag.String("lib", "/data/local/share/echomuse/alarm/libonnxruntime.so", "ONNX Runtime library")
	model := flag.String("model", "/data/local/share/echomuse/alarm/yamnet_classifier.onnx", "log-mel-to-scores ONNX model")
	classes := flag.String("classes", "/data/local/share/echomuse/alarm/yamnet_class_map.csv", "YAMNet class map")
	fixture := flag.String("fixture", "", "16kHz mono S16LE PCM fixture (required)")
	seconds := flag.Int("seconds", 180, "paced benchmark duration")
	flag.Parse()
	if *fixture == "" {
		fmt.Fprintln(os.Stderr, "yamnet_probe: -fixture is required")
		os.Exit(2)
	}
	if err := run(*lib, *model, *classes, *fixture, *seconds); err != nil {
		fmt.Fprintln(os.Stderr, "yamnet_probe:", err)
		os.Exit(1)
	}
}

func run(lib, model, classes, fixture string, seconds int) error {
	f, err := os.Open(classes)
	if err != nil {
		return err
	}
	cm, err := soundevent.ParseClassMap(f)
	f.Close()
	if err != nil {
		return err
	}
	b, err := os.ReadFile(fixture)
	if err != nil {
		return err
	}
	if len(b) < soundevent.WindowSamples*2 {
		return fmt.Errorf("fixture is shorter than 975ms")
	}
	pcm := make([]float32, len(b)/2)
	for i := range pcm {
		pcm[i] = float32(int16(binary.LittleEndian.Uint16(b[i*2:]))) / 32768
	}
	rt, err := ort.Open(lib)
	if err != nil {
		return err
	}
	sess, err := rt.NewSession(model, "yamnet", ort.DefaultOptions())
	if err != nil {
		return err
	}
	defer sess.Close()
	fmt.Printf("runtime   onnxruntime %s\n", rt.Version())
	fmt.Printf("session   threads=1 xnnpack=%v spinning=false\n", sess.XNNPACKActive())
	features, err := soundevent.LogMel(pcm[:soundevent.WindowSamples])
	if err != nil {
		return err
	}
	out, err := sess.Run(features, []int64{1, soundevent.PatchFrames, soundevent.MelBands})
	if err != nil {
		return err
	}
	if len(out) != soundevent.ExpectedClasses {
		return fmt.Errorf("output width %d, want %d", len(out), soundevent.ExpectedClasses)
	}
	for _, v := range out {
		if math.IsNaN(float64(v)) || math.IsInf(float64(v), 0) {
			return fmt.Errorf("non-finite output")
		}
	}
	scores, err := cm.Scores(out)
	if err != nil {
		return err
	}
	names := make([]string, 0, len(scores))
	for n := range scores {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		fmt.Printf("target    %-30s %.6f\n", n, scores[n])
	}

	frames := seconds * 1000 / 480
	lat := make([]time.Duration, 0, frames)
	late := 0
	var ru0, ru1 syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru0); err != nil {
		return err
	}
	start := time.Now()
	for i := 0; i < frames; i++ {
		off := (i * soundevent.HopSamples) % (len(pcm) - soundevent.WindowSamples + 1)
		t0 := time.Now()
		features, err := soundevent.LogMel(pcm[off : off+soundevent.WindowSamples])
		if err != nil {
			return err
		}
		if _, err := sess.Run(features, []int64{1, soundevent.PatchFrames, soundevent.MelBands}); err != nil {
			return err
		}
		lat = append(lat, time.Since(t0))
		next := start.Add(time.Duration(i+1) * 480 * time.Millisecond)
		if wait := time.Until(next); wait > 0 {
			time.Sleep(wait)
		} else {
			late++
		}
	}
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru1); err != nil {
		return err
	}
	sort.Slice(lat, func(i, j int) bool { return lat[i] < lat[j] })
	p := func(q float64) time.Duration { return lat[int(float64(len(lat)-1)*q)] }
	wall := time.Since(start)
	cpu := tv(ru1.Utime) + tv(ru1.Stime) - tv(ru0.Utime) - tv(ru0.Stime)
	fmt.Printf("frames    %d over %.1fs; deadline overruns %d\n", frames, wall.Seconds(), late)
	fmt.Printf("latency   p50 %.1fms p95 %.1fms max %.1fms (budget 480ms)\n", ms(p(.5)), ms(p(.95)), ms(lat[len(lat)-1]))
	fmt.Printf("CPU       %.1f%% of one core; RSS %d KB peak\n", cpu/wall.Seconds()*100, ru1.Maxrss)
	if late != 0 {
		return fmt.Errorf("VERDICT: fail — %d deadline overruns", late)
	}
	fmt.Println("VERDICT: pass — shape, finite scores and cadence are valid")
	return nil
}

func tv(t syscall.Timeval) float64 { return float64(t.Sec) + float64(t.Usec)/1e6 }
func ms(d time.Duration) float64   { return float64(d) / float64(time.Millisecond) }
