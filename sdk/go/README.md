# market-data-service Go SDK

HTTP client for the `market-data-service` proxy. Mirrors the routers
exposed by the FastAPI app:

- `GET /constituents?etf={ETF}&date={YYYY-MM-DD}`
- `GET /market-data/{ticker}?from={YYYY-MM-DD}&to={YYYY-MM-DD}`
- `GET /market-calendar?date={YYYY-MM-DD}`

## Install

```bash
go get github.com/marcuspeh/market-data-service/sdk/go
```

## Quick start

```go
package main

import (
	"context"
	"log"
	"time"

	marketdata "github.com/marcuspeh/market-data-service/sdk/go"
)

func main() {
	ctx := context.Background()

	client := marketdata.NewClient("http://localhost:3556")

	constituents, err := client.GetConstituents(ctx, "SPY", time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC))
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("%s on %s: %d holdings", constituents.Symbol, constituents.Date, len(constituents.Constituents))

	bars, err := client.GetBars(ctx, "AAPL",
		time.Date(2026, 8, 10, 0, 0, 0, 0, time.UTC),
		time.Date(2026, 8, 20, 0, 0, 0, 0, time.UTC),
	)
	if err != nil {
		log.Fatal(err)
	}
	for _, b := range bars.Bars {
		// A bar has no Date field — its identity is the timestamp, which
		// for a daily bar is NY midnight.
		day := time.UnixMilli(b.Timestamp).UTC().Format("2006-01-02")
		log.Printf("%s %s close=%.2f source=%s", b.Ticker, day, b.Close, b.Source)
	}

	// Was 2026-11-27 a market holiday or a shortened session?
	session, err := client.GetMarketCalendar(ctx, time.Date(2026, 11, 27, 0, 0, 0, 0, time.UTC))
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("%s status=%s earlyClose=%v", session.DateOnly(), session.Status, session.IsEarlyClose)
}
```

`NewClient("")` (or `NewClient("http://localhost:3556")`) targets the
local default. Point it at another host by passing a base URL.

## Date handling

`GetConstituents`, `GetBars` and `GetMarketCalendar` take `time.Time`
arguments; the SDK formats them as `YYYY-MM-DD` (calendar day) before
sending. Pass any `time.Time` in any zone — only the date component
matters on the wire.

`GetBars(ctx, ticker, from, to.Time)` — pass the zero `time.Time{}` for
`to` to omit the upper bound and let the proxy default to today (NY).

Response dates come back as **strings**, not `time.Time`: the proxy
emits RFC3339, but the SDK leaves them as-is so the exact wire value is
visible.

## Response types

```go
type ConstituentResponse struct {
	Symbol       string
	Date         string
	Constituents []string
	Source       string
}

type BarsResponse struct {
	Ticker         string
	From           string
	To             string
	BackfilledBars int
	Bars           []Bar
}

type Bar struct {
	Ticker     string
	Timestamp  int64   // epoch milliseconds (Nasdaq calendar day, NY tz)
	Open       float64
	High       float64
	Low        float64
	Close      float64
	Volume     float64
	VWAP       *float64
	TradeCount *int
	Source     string // "cache" or "longbridge"
}

type MarketCalendarResponse struct {
	Date         string  // UTC-midnight RFC3339 marker
	Status       string  // "regular", "early_close", "holiday", "weekend"
	IsTradingDay bool
	IsHoliday    bool
	IsEarlyClose bool
	Open         *string // RFC3339 with the US/Eastern offset; nil if no session
	Close        *string // RFC3339 with the US/Eastern offset; nil if no session
}
```

`Bar.VWAP` and `Bar.TradeCount` are pointers so the zero value (`nil`)
distinguishes "absent" from 0. `MarketCalendarResponse.Open` and
`.Close` are pointers for the same reason — `nil` means "the exchange
did not hold a session that day".

`MarketCalendarResponse.Date` is a UTC-midnight marker
(`"2026-11-27T00:00:00Z"`), not an instant: parsed as a `time.Time` and
formatted in US/Eastern it gives the previous evening. Use
`DateOnly()` to get the day the endpoint actually answered for.

## Error handling

All methods return a plain `error`. The most common cases:

- **400** — unsupported ETF symbol, or a market-calendar date outside
  the supported range. The proxy's `detail` field is included in the
  error message.
- **404** — no constituents snapshot for the requested `(symbol, date)`.
- **5xx / network failure** — wrapped with the upstream `detail` if
  available, or the raw status code otherwise.

Inspect with `errors.Is` / `errors.As` as appropriate, or just log the
string.
