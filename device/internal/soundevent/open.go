package soundevent

import (
	"fmt"
	"math"
	"os"
	"path/filepath"

	"github.com/wilbowes/EchoMuse/internal/wakeword/ort"
)

const DefaultDir = "/data/local/share/echomuse/alarm"

func Dir() string {
	if d := os.Getenv("EM_SOUND_EVENT_DIR"); d != "" {
		return d
	}
	return DefaultDir
}

type ortInferer struct {
	sess  *ort.Session
	model string
}

func (i *ortInferer) Infer(in []float32) ([]float32, error) {
	features, err := LogMel(in)
	if err != nil {
		return nil, err
	}
	return i.sess.Run(features, []int64{1, PatchFrames, MelBands})
}
func (i *ortInferer) Close() error { return i.sess.Close() }
func (i *ortInferer) Info() (string, string, bool) {
	return "onnxruntime " + i.sess.RuntimeVersion(), i.model, i.sess.XNNPACKActive()
}

// Open loads the out-of-band runtime, neural-only ONNX model and official
// class manifest. Their absence is returned as an ordinary error so the caller
// can leave detection disabled without affecting device startup.
func Open(cfg Config, playback func() bool, onEvent func(Event)) (*Detector, error) {
	dir := Dir()
	modelPath := filepath.Join(dir, "yamnet_classifier.onnx")
	mapPath := filepath.Join(dir, "yamnet_class_map.csv")
	libPath := filepath.Join(dir, "libonnxruntime.so")
	for what, path := range map[string]string{
		"runtime": libPath, "model": modelPath, "class map": mapPath,
	} {
		if _, err := os.Stat(path); err != nil {
			return nil, fmt.Errorf("soundevent: %s not installed at %s", what, path)
		}
	}
	f, err := os.Open(mapPath)
	if err != nil {
		return nil, fmt.Errorf("soundevent: open class map: %w", err)
	}
	classes, err := ParseClassMap(f)
	f.Close()
	if err != nil {
		return nil, fmt.Errorf("soundevent: %w", err)
	}
	rt, err := ort.Open(libPath)
	if err != nil {
		return nil, fmt.Errorf("soundevent: %w", err)
	}
	sess, err := rt.NewSession(modelPath, "yamnet", ort.DefaultOptions())
	if err != nil {
		return nil, fmt.Errorf("soundevent: %w", err)
	}
	inf := &ortInferer{sess: sess, model: filepath.Base(modelPath)}
	probe, err := inf.Infer(make([]float32, WindowSamples))
	if err != nil {
		_ = inf.Close()
		return nil, fmt.Errorf("soundevent: model validation: %w", err)
	}
	if len(probe) != ExpectedClasses {
		_ = inf.Close()
		return nil, fmt.Errorf("soundevent: model validation produced %d classes, want %d", len(probe), ExpectedClasses)
	}
	for _, v := range probe {
		if math.IsNaN(float64(v)) || math.IsInf(float64(v), 0) {
			_ = inf.Close()
			return nil, fmt.Errorf("soundevent: model validation produced non-finite scores")
		}
	}
	d, err := New(inf, classes, cfg, playback, onEvent)
	if err != nil {
		_ = inf.Close()
		return nil, err
	}
	return d, nil
}
