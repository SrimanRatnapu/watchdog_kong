// watchdog-audit: a Kong plugin (Go PDK) that writes every model call Watchdog makes into a tamper-evident chain.
//
// Per request it records who called (the Kong consumer, i.e. Watchdog's role), which case it was for (the
// X-Watchdog-Case header, removed before the request goes upstream), the model, the status, whether Kong answered
// itself (a prompt-guard block), and SHA-256 hashes of the request and response bodies. Bodies are never stored.
// Entries are hash-chained (chain.go): an investigator who receives a case can check that the log of the AI calls
// behind it was not edited afterwards (scripts/verify_audit_chain.py).
//
// Priority 1000 runs it after key-auth (1250), so the consumer is known, and before ai-prompt-guard and ai-proxy,
// so a blocked request is still hashed. The log phase runs for every request, blocked ones included.
package main

import (
	"encoding/json"
	"log"
	"time"

	"github.com/Kong/go-pdk"
	"github.com/Kong/go-pdk/server"
)

const (
	Version     = "0.1.0"
	Priority    = 1000
	CaseHeader  = "X-Watchdog-Case"
	defaultPath = "/kong/audit/chain.jsonl"
)

// Config is the plugin's configuration in kong.yml.
type Config struct {
	Path string `json:"path"` // where the chain is written; default /kong/audit/chain.jsonl
}

func New() interface{} { return &Config{} }

func (c Config) path() string {
	if c.Path == "" {
		return defaultPath
	}
	return c.Path
}

func (c Config) Access(kong *pdk.PDK) {
	body, err := kong.Request.GetRawBody()
	if err != nil {
		kong.Log.Warn("watchdog-audit: request body unavailable: ", err.Error())
	}
	var req struct {
		Model string `json:"model"`
	}
	_ = json.Unmarshal(body, &req)
	caseID, _ := kong.Request.GetHeader(CaseHeader)
	_ = kong.Ctx.SetShared("wd_req_sha", SHA256Hex(body))
	_ = kong.Ctx.SetShared("wd_model", req.Model)
	_ = kong.Ctx.SetShared("wd_case", caseID)
	_ = kong.ServiceRequest.ClearHeader(CaseHeader) // ours, not Anthropic's
}

// Response runs only when the model answered (not for a request Kong refused itself).
func (c Config) Response(kong *pdk.PDK) {
	if body, err := kong.ServiceResponse.GetRawBody(); err == nil {
		_ = kong.Ctx.SetShared("wd_resp_sha", SHA256Hex(body))
	}
}

func (c Config) Log(kong *pdk.PDK) {
	shared := func(k string) string { v, _ := kong.Ctx.GetSharedString(k); return v }
	consumer, _ := kong.Client.GetConsumer()
	status, _ := kong.Response.GetStatus()
	source, _ := kong.Response.GetSource()
	e := Entry{
		At:             time.Now().UTC().Format(time.RFC3339Nano),
		Consumer:       consumer.Username,
		CaseID:         shared("wd_case"),
		Model:          shared("wd_model"),
		Status:         status,
		Source:         source,
		Blocked:        source != "service" && status == 400,
		RequestSHA256:  shared("wd_req_sha"),
		ResponseSHA256: shared("wd_resp_sha"),
	}
	chain, err := ChainAt(c.path())
	if err != nil {
		kong.Log.Err("watchdog-audit: ", err.Error())
		return
	}
	if _, err := chain.Append(e); err != nil {
		kong.Log.Err("watchdog-audit: could not append: ", err.Error())
	}
}

func main() {
	if err := server.StartServer(New, Version, Priority); err != nil {
		log.Fatal(err)
	}
}
