package marketdata

import "time"

// ConstituentResponse matches the JSON shape returned by the
// market-data-service `/constituents` endpoint.
type ConstituentResponse struct {
	Symbol       string   `json:"symbol"`
	Date         string   `json:"date"`
	Constituents []string `json:"constituents"`
	Source       string   `json:"source"`
}

// BarsResponse matches the JSON shape returned by the
// market-data-service `/market-data/{ticker}` endpoint.
type BarsResponse struct {
	Ticker          string `json:"ticker"`
	From            string `json:"from"`
	To              string `json:"to"`
	BackfilledBars  int    `json:"backfilled_bars"`
	Bars            []Bar  `json:"bars"`
}

// Bar is the per-day OHLCV payload returned by the proxy. We keep this
// type local so callers can convert into their own indicator-friendly
// Bar (e.g. indicators.Bar) at the boundary.
type Bar struct {
	Ticker      string    `json:"ticker"`
	Date        time.Time `json:"date"`
	Timestamp   int64     `json:"timestamp"`
	Open        float64   `json:"open"`
	High        float64   `json:"high"`
	Low         float64   `json:"low"`
	Close       float64   `json:"close"`
	Volume      float64   `json:"volume"`
	VWAP        *float64  `json:"vwap,omitempty"`
	TradeCount  *int      `json:"trade_count,omitempty"`
	Source      string    `json:"source"`
}
