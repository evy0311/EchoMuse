package soundevent

import (
	"encoding/csv"
	"fmt"
	"io"
	"strconv"
	"strings"
)

const ExpectedClasses = 521

const (
	ClassSmokeAlarm = "Smoke detector, smoke alarm"
	ClassFireAlarm  = "Fire alarm"
	ClassSiren      = "Siren"
	ClassBuzzer     = "Buzzer"
	ClassAlarm      = "Alarm"
	ClassGlass      = "Glass"
	ClassCrack      = "Crack"
	ClassShatter    = "Shatter"
)

var targetNames = []string{
	ClassSmokeAlarm, ClassFireAlarm, ClassSiren, ClassBuzzer,
	ClassAlarm, ClassGlass, ClassCrack, ClassShatter,
}

// ClassMap is the validated mapping shipped beside the model. Numeric indices
// are never silently assumed: startup verifies every row and resolves targets
// by their official AudioSet display names.
type ClassMap struct {
	Names   []string
	Targets map[string]int
}

func ParseClassMap(r io.Reader) (ClassMap, error) {
	rows, err := csv.NewReader(r).ReadAll()
	if err != nil {
		return ClassMap{}, fmt.Errorf("class map: %w", err)
	}
	if len(rows) != ExpectedClasses+1 {
		return ClassMap{}, fmt.Errorf("class map: got %d classes, want %d", len(rows)-1, ExpectedClasses)
	}
	if len(rows[0]) < 3 || strings.TrimSpace(rows[0][0]) != "index" ||
		strings.TrimSpace(rows[0][2]) != "display_name" {
		return ClassMap{}, fmt.Errorf("class map: unexpected header %q", rows[0])
	}
	cm := ClassMap{Names: make([]string, ExpectedClasses), Targets: map[string]int{}}
	for i, row := range rows[1:] {
		if len(row) < 3 {
			return ClassMap{}, fmt.Errorf("class map: row %d has %d fields", i+2, len(row))
		}
		idx, err := strconv.Atoi(strings.TrimSpace(row[0]))
		if err != nil || idx != i {
			return ClassMap{}, fmt.Errorf("class map: row %d index %q, want %d", i+2, row[0], i)
		}
		name := strings.TrimSpace(row[2])
		if name == "" {
			return ClassMap{}, fmt.Errorf("class map: row %d has empty display name", i+2)
		}
		cm.Names[i] = name
	}
	for _, name := range targetNames {
		for i, got := range cm.Names {
			if got == name {
				cm.Targets[name] = i
				break
			}
		}
		if _, ok := cm.Targets[name]; !ok {
			return ClassMap{}, fmt.Errorf("class map: required class %q is absent", name)
		}
	}
	return cm, nil
}

func (m ClassMap) Scores(output []float32) (map[string]float32, error) {
	if len(output) != len(m.Names) || len(output) != ExpectedClasses {
		return nil, fmt.Errorf("model output has %d classes, want %d", len(output), ExpectedClasses)
	}
	out := make(map[string]float32, len(m.Targets))
	for name, idx := range m.Targets {
		out[name] = output[idx]
	}
	return out, nil
}
