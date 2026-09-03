# market-data-service Go SDK

HTTP client for the `market-data-service` proxy. Mirrors the two routers
exposed by the FastAPI app:

- `GET /constituents?etf={ETF}&date={YYYY-MM-DD}`
- `GET /market-data/{ticker}?from={YYYY-MM-DD}&to={YYYY-MM-DD}`

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
		log.Printf("%s %s close=%.2f source=%s", b.Ticker, b.Date.Format("2006-01-02"), b.Close, b.Source)
	}
}
```

`NewClient("")` (or `NewClient("http://localhost:3556")`) targets the
local default. Point it at another host by passing a base URL.

## Date handling

`GetConstituents` and `GetBars` take `time.Time` arguments; the SDK
formats them as `YYYY-MM-DD` (calendar day) before sending. Pass any
`time.Time` in any zone — only the date component matters on the wire.

`GetBars(ctx, ticker, from, to.Time)` — pass the zero `time.Time{}` for
`to` to omit the upper bound and let the proxy default to today (NY).

## Response types

```go
type ConstituentResponse struct {
	Symbol       string
	Date         time.Time
	Constituents []string
	Source       string
}

type BarsResponse struct {
	Ticker         string
	From           time.Time
	To             time.Time
	BackfilledBars int
	Bars           []Bar
}

type Bar struct {
	Ticker     string
	Date       time.Time
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
```

`Bar.VWAP` and `Bar.TradeCount` are pointers so the zero value (`nil`)
distinguishes "absent" from 0.

## Error handling

Both methods return a plain `error`. The most common cases:

- **400** — unsupported ETF symbol. The proxy's `detail` field is
  included in the error message.
- **404** — no constituents snapshot for the requested `(symbol, date)`.
- **5xx / network failure** — wrapped with the upstream `detail` if
  available, or the raw status code otherwise.

Inspect with `errors.Is` / `errors.As` as appropriate, or just log the
string.
