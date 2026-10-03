package soundevent

import (
	"crypto/rand"
	"encoding/binary"
	"fmt"
	"math"
	"sync"
	"sync/atomic"
	"time"
)

const (
	SampleRate    = 16000
	WindowSamples = 15600 // 0.975s: enough for YAMNet's first 0.96s patch.
	HopSamples    = 7680  // 0.48s.
	queueFrames   = 16    // 1.28s of 80ms capture frames.
)

const (
	ModeOff    = "off"
	ModeShadow = "shadow"
	ModeOn     = "on"
)

const (
	KindSmokeAlarm = "smoke_alarm"
	KindFireAlarm  = "fire_alarm"
	KindPossibleCO = "possible_co_alarm"
	KindGlassBreak = "glass_break"
	KindUnknown    = "alarm_unknown"
)

type Config struct {
	Mode          string
	Threshold     float32
	Confirmations int
	Cooldown      time.Duration
}

func (c Config) normalised() Config {
	if c.Mode != ModeShadow && c.Mode != ModeOn {
		c.Mode = ModeOff
	}
	if c.Threshold <= 0 || c.Threshold > 1 {
		c.Threshold = 0.5
	}
	if c.Confirmations < 1 {
		c.Confirmations = 2
	}
	if c.Cooldown <= 0 {
		c.Cooldown = 60 * time.Second
	}
	return c
}

type Event struct {
	Kind          string             `json:"kind"`
	Confidence    float32            `json:"confidence"`
	Scores        map[string]float32 `json:"scores"`
	Mode          string             `json:"mode"`
	Confirmations int                `json:"confirmations"`
	Cadence       string             `json:"cadence"`
	Evidence      map[string]any     `json:"evidence,omitempty"`
	Playback      bool               `json:"playbackActive"`
	CapturedAt    time.Time          `json:"-"`
	Sequence      uint64             `json:"sequence"`
	Model         string             `json:"model"`
	Runtime       string             `json:"runtime"`
}

type Stats struct {
	Windows    uint64  `json:"windows"`
	Drops      uint64  `json:"drops"`
	Errors     uint64  `json:"errors"`
	LastErr    string  `json:"lastErr,omitempty"`
	MaxInferMs uint32  `json:"maxInferMs"`
	Threshold  float32 `json:"threshold"`
	MaxScore   float32 `json:"maxScore"`
	Mode       string  `json:"mode"`
	Runtime    string  `json:"runtime"`
	Model      string  `json:"model"`
	XNNPACK    bool    `json:"xnnpack"`
}

type Inferer interface {
	Infer([]float32) ([]float32, error)
	Close() error
	Info() (runtime, model string, xnnpack bool)
}

type queued struct {
	pcm []int16
	at  time.Time
}

type candidateState struct {
	count int
	last  time.Time
}

// Detector owns buffering, inference, confirmation and cooldowns. Push is the
// only method called from the microphone goroutine and never waits.
type Detector struct {
	inf      Inferer
	classes  ClassMap
	playback func() bool
	onEvent  func(Event)

	ch     chan queued
	quit   chan struct{}
	done   chan struct{}
	stop   sync.Once
	closed atomic.Bool
	drops  atomic.Uint64
	maxMs  atomic.Uint32

	mu         sync.Mutex
	cfg        Config
	stats      Stats
	candidates map[string]candidateState
	lastEvent  map[string]time.Time
	lastPush   time.Time

	cadence CadenceVerifier
	floor   float64
}

// eventSequence starts each daemon in a random signed-64-bit range and then
// increases monotonically. Keeping it outside Detector prevents an off/on
// config cycle from reusing keys; the random high bits make a daemon restart
// in the same kernel boot negligibly likely to collide with earlier events.
var eventSequence atomic.Uint64

func init() {
	var seedBytes [8]byte
	if _, err := rand.Read(seedBytes[:]); err != nil {
		binary.LittleEndian.PutUint64(seedBytes[:], uint64(time.Now().UnixNano()))
	}
	// Reserve 32 low bits for the counter and keep SQLite's sign bit clear.
	eventSequence.Store(binary.LittleEndian.Uint64(seedBytes[:]) & 0x7fffffff00000000)
}

func New(inf Inferer, classes ClassMap, cfg Config, playback func() bool, onEvent func(Event)) (*Detector, error) {
	if inf == nil {
		return nil, fmt.Errorf("soundevent: nil inferer")
	}
	runtime, model, xnn := inf.Info()
	if len(classes.Names) != ExpectedClasses {
		return nil, fmt.Errorf("soundevent: unvalidated class map")
	}
	d := &Detector{
		inf: inf, classes: classes, cfg: cfg.normalised(), playback: playback,
		onEvent: onEvent, ch: make(chan queued, queueFrames), quit: make(chan struct{}),
		done: make(chan struct{}), candidates: map[string]candidateState{},
		lastEvent: map[string]time.Time{}, floor: 0.001,
	}
	d.stats.Runtime, d.stats.Model, d.stats.XNNPACK = runtime, model, xnn
	go d.run()
	return d, nil
}

func (d *Detector) SetConfig(cfg Config) {
	d.mu.Lock()
	d.cfg = cfg.normalised()
	d.mu.Unlock()
}

func (d *Detector) PushBytesAt(b []byte, at time.Time) {
	if d == nil || d.closed.Load() || len(b) < 2 {
		return
	}
	pcm := make([]int16, len(b)/2)
	for i := range pcm {
		pcm[i] = int16(binary.LittleEndian.Uint16(b[i*2:]))
	}
	q := queued{pcm: pcm, at: at}
	select {
	case d.ch <- q:
		return
	default:
	}
	// Prefer current audio: evict one stale item. A racing consumer may empty
	// the channel between selects; either outcome stays non-blocking.
	select {
	case <-d.ch:
		d.drops.Add(1)
	default:
	}
	select {
	case d.ch <- q:
	default:
		d.drops.Add(1)
	}
}

func (d *Detector) Drain() Stats {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := d.stats
	d.stats.Windows, d.stats.Errors, d.stats.MaxScore = 0, 0, 0
	d.stats.LastErr = ""
	out.Drops = d.drops.Swap(0)
	out.MaxInferMs = d.maxMs.Swap(0)
	out.Threshold, out.Mode = d.cfg.Threshold, d.cfg.Mode
	return out
}

func (d *Detector) Close() {
	if d == nil {
		return
	}
	d.stop.Do(func() {
		d.closed.Store(true)
		close(d.quit)
		<-d.done
		_ = d.inf.Close()
	})
}

func (d *Detector) run() {
	defer close(d.done)
	buf := make([]int16, 0, WindowSamples+1280)
	var windowAt time.Time
	for {
		select {
		case <-d.quit:
			return
		case q := <-d.ch:
			d.observeEnergy(q)
			buf = append(buf, q.pcm...)
			windowAt = q.at
			for len(buf) >= WindowSamples {
				window := make([]float32, WindowSamples)
				for i, v := range buf[:WindowSamples] {
					window[i] = float32(v) / 32768.0
				}
				d.infer(window, windowAt)
				buf = buf[HopSamples:]
			}
		}
	}
}

func (d *Detector) observeEnergy(q queued) {
	if len(q.pcm) == 0 {
		return
	}
	var sum float64
	for _, v := range q.pcm {
		x := float64(v) / 32768.0
		sum += x * x
	}
	rms := math.Sqrt(sum / float64(len(q.pcm)))
	bar := math.Max(0.015, d.floor*5)
	loud := rms >= bar
	if !loud {
		d.floor = d.floor*0.995 + rms*0.005
	}
	d.cadence.Observe(loud, q.at)
}

func (d *Detector) infer(window []float32, at time.Time) {
	t0 := time.Now()
	out, err := d.inf.Infer(window)
	ms := uint32(time.Since(t0).Milliseconds())
	for old := d.maxMs.Load(); ms > old && !d.maxMs.CompareAndSwap(old, ms); old = d.maxMs.Load() {
	}
	if err != nil {
		d.recordErr(err)
		return
	}
	for _, v := range out {
		if math.IsNaN(float64(v)) || math.IsInf(float64(v), 0) {
			d.recordErr(fmt.Errorf("non-finite model output"))
			return
		}
	}
	scores, err := d.classes.Scores(out)
	if err != nil {
		d.recordErr(err)
		return
	}
	d.evaluate(scores, at)
}

func (d *Detector) recordErr(err error) {
	d.mu.Lock()
	d.stats.Errors++
	d.stats.LastErr = err.Error()
	d.mu.Unlock()
}

func maxScore(scores map[string]float32) float32 {
	var m float32
	for _, v := range scores {
		if v > m {
			m = v
		}
	}
	return m
}

func (d *Detector) evaluate(scores map[string]float32, at time.Time) {
	d.mu.Lock()
	defer d.mu.Unlock()
	cfg := d.cfg
	d.stats.Windows++
	peak := maxScore(scores)
	if peak > d.stats.MaxScore {
		d.stats.MaxScore = peak
	}
	if cfg.Mode == ModeOff {
		return
	}
	playback := d.playback != nil && d.playback()
	bar, need := cfg.Threshold, cfg.Confirmations
	if playback {
		bar = min(float32(0.99), bar+0.15)
		need++
	}
	kind, confidence := classify(scores, bar, d.cadence.current(at))
	if kind == "" {
		for k := range d.candidates {
			delete(d.candidates, k)
		}
		return
	}
	st := d.candidates[kind]
	if !st.last.IsZero() && at.Sub(st.last) > 1100*time.Millisecond {
		st.count = 0
	}
	st.count++
	st.last = at
	d.candidates = map[string]candidateState{kind: st}
	if st.count < need || at.Sub(d.lastEvent[kind]) < cfg.Cooldown {
		return
	}
	d.lastEvent[kind] = at
	d.candidates[kind] = candidateState{}
	runtime, model, _ := d.inf.Info()
	e := Event{
		Kind: kind, Confidence: confidence, Scores: scores, Mode: cfg.Mode,
		Confirmations: st.count, Cadence: d.cadence.current(at), Playback: playback,
		CapturedAt: at, Sequence: eventSequence.Add(1), Runtime: runtime, Model: model,
		Evidence: map[string]any{"threshold": bar, "windowMs": 975, "hopMs": 480},
	}
	if d.onEvent != nil {
		// Callback must be non-blocking by contract (the control client's write
		// queue is buffered), just like the wake-word crossing callback.
		d.onEvent(e)
	}
}

func classify(s map[string]float32, bar float32, cadence string) (string, float32) {
	smoke, fire := s[ClassSmokeAlarm], s[ClassFireAlarm]
	if cadence == CadenceT4 && max(s[ClassAlarm], s[ClassSiren], s[ClassBuzzer], smoke, fire) >= bar {
		return KindPossibleCO, max(s[ClassAlarm], s[ClassSiren], s[ClassBuzzer], smoke, fire)
	}
	if smoke >= bar || fire >= bar {
		if fire > smoke {
			return KindFireAlarm, fire
		}
		return KindSmokeAlarm, smoke
	}
	shatter := s[ClassShatter]
	if shatter >= bar && (s[ClassGlass] >= bar*0.45 || s[ClassCrack] >= bar*0.45) {
		return KindGlassBreak, shatter
	}
	generic := max(s[ClassAlarm], s[ClassSiren], s[ClassBuzzer])
	if generic >= bar {
		return KindUnknown, generic
	}
	return "", 0
}

func max(v ...float32) float32 {
	var out float32
	for _, n := range v {
		if n > out {
			out = n
		}
	}
	return out
}
