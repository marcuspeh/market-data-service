package marketdata

// ConstituentResponse matches the JSON shape returned by the
// market-data-service `/constituents` endpoint.
type ConstituentResponse struct {
	Symbol       string   `json:"symbol"`
	Date         string   `json:"date"`
	Constituents []string `json:"constituents"`
	Source       string   `json:"source"`
}

// MarketCalendarResponse matches the JSON shape returned by the
// market-data-service `/market-calendar` endpoint.
//
// Invariants relied on by callers:
//   - IsTradingDay is true exactly when Status is "regular" or
//     "early_close".
//   - IsEarlyClose implies IsTradingDay.
//   - IsHoliday means "a weekday on which the exchange did not trade" —
//     a weekend is not a holiday.
//   - Open and Close are set exactly when the exchange held a session.
//
// Open and Close are pointers so the zero value (nil) distinguishes
// "no session" from an empty string.
type MarketCalendarResponse struct {
	Date         string  `json:"date"`
	Status       string  `json:"status"`
	IsTradingDay bool    `json:"is_trading_day"`
	IsHoliday    bool    `json:"is_holiday"`
	IsEarlyClose bool    `json:"is_early_close"`
	Open         *string `json:"open"`
	Close        *string `json:"close"`
}

// DateOnly returns the calendar day encoded in Date, in YYYY-MM-DD form.
//
// Date is a UTC-midnight marker ("2026-11-27T00:00:00Z"), not an
// instant: parsed as a time.Time and formatted in US/Eastern it yields
// the previous evening. Use this when you want the day the endpoint
// actually answered for.
func (r *MarketCalendarResponse) DateOnly() string {
	if len(r.Date) >= 10 {
		return r.Date[:10]
	}
	return r.Date
}

// BarsResponse matches the JSON shape returned by the
// market-data-service `/market-data/{ticker}` endpoint.
type BarsResponse struct {
	Ticker         string `json:"ticker"`
	From           string `json:"from"`
	To             string `json:"to"`
	BackfilledBars int    `json:"backfilled_bars"`
	Bars           []Bar  `json:"bars"`
}

// Bar is the per-bar OHLCV payload returned by the proxy. We keep this
// type local so callers can convert into their own indicator-friendly
// Bar (e.g. indicators.Bar) at the boundary.
//
// Timestamp is the bar close time in epoch milliseconds. Daily bars
// carry NY midnight; minute / hour bars carry the actual close time.
// There is no separate Date field — derive a calendar day from
// Timestamp if needed.
type Bar struct {
	Ticker     string   `json:"ticker"`
	Timestamp  int64    `json:"timestamp"`
	Open       float64  `json:"open"`
	High       float64  `json:"high"`
	Low        float64  `json:"low"`
	Close      float64  `json:"close"`
	Volume     float64  `json:"volume"`
	VWAP       *float64 `json:"vwap,omitempty"`
	TradeCount *int     `json:"trade_count,omitempty"`
	Source     string   `json:"source"`
}
