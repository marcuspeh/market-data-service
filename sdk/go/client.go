package marketdata

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"time"
)

// Client is a thin HTTP client for the market-data-service proxy.
// It mirrors the two routers exposed by ../market-data-service/app/main.py:
//   - GET /constituents?etf=SPY&date=YYYY-MM-DD
//   - GET /market-data/{ticker}?from=YYYY-MM-DD&to=YYYY-MM-DD
type Client struct {
	baseURL string
}

// NewClient returns a Client targeting the market-data-service proxy.
// An empty baseURL defaults to the local default (http://localhost:8001).
func NewClient(baseURL string) *Client {
	if baseURL == "" {
		baseURL = "http://localhost:8001"
	}
	return &Client{baseURL: baseURL}
}

func (c *Client) do(ctx context.Context, req *http.Request, out interface{}) error {
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("failed to fetch from remote: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		var errResp struct {
			Detail string `json:"detail"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&errResp); err == nil && errResp.Detail != "" {
			return fmt.Errorf("remote server returned error: %s", errResp.Detail)
		}
		return fmt.Errorf("remote server returned status: %d", resp.StatusCode)
	}

	if out != nil {
		if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
			return fmt.Errorf("failed to decode response: %w", err)
		}
	}
	return nil
}

// GetConstituents returns the constituents snapshot for the given ETF
// symbol on the given date. `date` is interpreted as a calendar day; the
// proxy expects YYYY-MM-DD.
func (c *Client) GetConstituents(ctx context.Context, symbol string, date time.Time) (*ConstituentResponse, error) {
	q := url.Values{}
	q.Set("etf", symbol)
	q.Set("date", date.Format("2006-01-02"))

	endpoint := fmt.Sprintf("%s/constituents?%s", c.baseURL, q.Encode())
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	var result ConstituentResponse
	if err := c.do(ctx, req, &result); err != nil {
		return nil, err
	}
	return &result, nil
}

// GetBars returns daily OHLCV bars for `ticker` between `from` (inclusive)
// and `to` (inclusive, defaults to today on the proxy side).
func (c *Client) GetBars(ctx context.Context, ticker string, from, to time.Time) (*BarsResponse, error) {
	q := url.Values{}
	q.Set("from", from.Format("2006-01-02"))
	if !to.IsZero() {
		q.Set("to", to.Format("2006-01-02"))
	}

	endpoint := fmt.Sprintf("%s/market-data/%s?%s", c.baseURL, url.PathEscape(ticker), q.Encode())
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	var result BarsResponse
	if err := c.do(ctx, req, &result); err != nil {
		return nil, err
	}
	return &result, nil
}
