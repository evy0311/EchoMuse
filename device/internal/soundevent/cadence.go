package soundevent

import "time"

const (
	CadenceT3      = "t3"
	CadenceT4      = "t4"
	CadenceUnknown = "unknown"
)

// CadenceVerifier deliberately says only T3/T4 pattern, never detector type.
// The caller may call a sufficiently supported T4 result "possible CO"; the
// cadence itself is not proof of what produced it.
type CadenceVerifier struct {
	loud          bool
	stateSince    time.Time
	bursts        int
	last          string
	lastAt        time.Time
	groupFinished bool
}

// Observe consumes the 80ms energy decision. A group is separated by at least
// 1.2s of quiet; pulse widths and within-group gaps outside 80..800ms make the
// group ambiguous. That wide tolerance is intentional for room echo and an
// 80ms observation grid.
func (c *CadenceVerifier) Observe(loud bool, at time.Time) string {
	if c.stateSince.IsZero() {
		c.loud, c.stateSince = loud, at
		if loud {
			c.bursts = 1
		}
		return c.current(at)
	}
	d := at.Sub(c.stateSince)
	if loud != c.loud {
		if c.loud {
			if d < 80*time.Millisecond || d > 800*time.Millisecond {
				c.bursts = 0
			}
		} else {
			if d >= 1200*time.Millisecond {
				c.finish(at)
				c.bursts = 1
			} else if d >= 80*time.Millisecond && d <= 800*time.Millisecond {
				c.bursts++
			} else {
				c.bursts = 1
			}
		}
		c.loud, c.stateSince = loud, at
		c.groupFinished = false
	} else if !loud && d >= 1200*time.Millisecond && !c.groupFinished {
		c.finish(at)
	}
	return c.current(at)
}

func (c *CadenceVerifier) finish(at time.Time) {
	switch c.bursts {
	case 3:
		c.last = CadenceT3
	case 4:
		c.last = CadenceT4
	default:
		c.last = CadenceUnknown
	}
	c.lastAt = at
	c.groupFinished = true
	c.bursts = 0
}

func (c *CadenceVerifier) current(at time.Time) string {
	if c.last == "" || at.Sub(c.lastAt) > 10*time.Second {
		return CadenceUnknown
	}
	return c.last
}
