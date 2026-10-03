package soundevent

import (
	"fmt"
	"math"
)

const (
	STFTWindow  = 400
	STFTHop     = 160
	FFTSize     = 512
	PatchFrames = 96
	MelBands    = 64
	MelMinHz    = 125.0
	MelMaxHz    = 7500.0
	LogOffset   = 0.001
)

var (
	hannWindow = makeHann()
	melWeights = makeMelWeights()
)

// LogMel implements YAMNet's published frontend exactly: periodic 25ms Hann
// windows, 10ms hop, 512-point magnitude STFT, 64 HTK mel bands from 125 to
// 7500Hz, and log(mel+0.001). Input is normalized mono 16kHz float audio.
func LogMel(waveform []float32) ([]float32, error) {
	if len(waveform) != WindowSamples {
		return nil, fmt.Errorf("yamnet frontend wants %d samples, got %d", WindowSamples, len(waveform))
	}
	out := make([]float32, PatchFrames*MelBands)
	re, im := make([]float64, FFTSize), make([]float64, FFTSize)
	mag := make([]float64, FFTSize/2+1)
	for frame := 0; frame < PatchFrames; frame++ {
		clear(re)
		clear(im)
		off := frame * STFTHop
		for i := 0; i < STFTWindow; i++ {
			re[i] = float64(waveform[off+i]) * hannWindow[i]
		}
		fft(re, im)
		for k := range mag {
			mag[k] = math.Hypot(re[k], im[k])
		}
		for m := 0; m < MelBands; m++ {
			var energy float64
			for k := range mag {
				energy += mag[k] * melWeights[k*MelBands+m]
			}
			out[frame*MelBands+m] = float32(math.Log(energy + LogOffset))
		}
	}
	return out, nil
}

func makeHann() []float64 {
	w := make([]float64, STFTWindow)
	for i := range w {
		w[i] = 0.5 - 0.5*math.Cos(2*math.Pi*float64(i)/STFTWindow)
	}
	return w
}

func hzToMel(hz float64) float64 { return 1127.0 * math.Log1p(hz/700.0) }

func makeMelWeights() []float64 {
	w := make([]float64, (FFTSize/2+1)*MelBands)
	lo, hi := hzToMel(MelMinHz), hzToMel(MelMaxHz)
	edges := make([]float64, MelBands+2)
	for i := range edges {
		edges[i] = lo + (hi-lo)*float64(i)/float64(MelBands+1)
	}
	for k := 0; k <= FFTSize/2; k++ {
		mel := hzToMel(float64(k) * SampleRate / FFTSize)
		for m := 0; m < MelBands; m++ {
			lower := (mel - edges[m]) / (edges[m+1] - edges[m])
			upper := (edges[m+2] - mel) / (edges[m+2] - edges[m+1])
			w[k*MelBands+m] = math.Max(0, math.Min(lower, upper))
		}
	}
	return w
}

// fft is an in-place unscaled radix-2 forward DFT, matching tf.signal.rfft.
func fft(re, im []float64) {
	n := len(re)
	for i, j := 1, 0; i < n; i++ {
		bit := n >> 1
		for ; j&bit != 0; bit >>= 1 {
			j ^= bit
		}
		j ^= bit
		if i < j {
			re[i], re[j], im[i], im[j] = re[j], re[i], im[j], im[i]
		}
	}
	for size := 2; size <= n; size <<= 1 {
		half := size >> 1
		angle := -2 * math.Pi / float64(size)
		for start := 0; start < n; start += size {
			for j := 0; j < half; j++ {
				c, s := math.Cos(angle*float64(j)), math.Sin(angle*float64(j))
				i, k := start+j, start+j+half
				tr, ti := c*re[k]-s*im[k], s*re[k]+c*im[k]
				re[k], im[k] = re[i]-tr, im[i]-ti
				re[i], im[i] = re[i]+tr, im[i]+ti
			}
		}
	}
}
