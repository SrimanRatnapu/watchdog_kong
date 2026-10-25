package main

// A hash-chained, append-only log of model calls. Each entry carries the hash of the entry before it, and its own
// hash covers every field (prev included), so changing, removing or reordering any entry breaks every hash after it.
// The chain logic has no Kong dependency, so it is tested on its own (chain_test.go) and mirrored in Python by
// scripts/verify_audit_chain.py.

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sync"
)

// Genesis is the prev of the first entry.
const Genesis = "0000000000000000000000000000000000000000000000000000000000000000"

// Entry is one model call as the gateway saw it. Hash is computed over all other fields.
type Entry struct {
	Seq            int    `json:"seq"`
	At             string `json:"at"`
	Consumer       string `json:"consumer"`
	CaseID         string `json:"case_id"`
	Model          string `json:"model"`
	Status         int    `json:"status"`
	Source         string `json:"source"` // "service": the model answered; "exit"/"error": Kong answered itself
	Blocked        bool   `json:"blocked"`
	RequestSHA256  string `json:"request_sha256"`
	ResponseSHA256 string `json:"response_sha256"` // empty when no response came back from the model
	Prev           string `json:"prev"`
	Hash           string `json:"hash"`
}

// canonical is the exact byte string an entry's hash covers: compact JSON, keys sorted, no HTML escaping, no "hash".
func canonical(e Entry) ([]byte, error) {
	raw, err := json.Marshal(e)
	if err != nil {
		return nil, err
	}
	var m map[string]any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&m); err != nil {
		return nil, err
	}
	delete(m, "hash")
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(m); err != nil { // map keys are sorted by encoding/json
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// HashOf is the hex SHA-256 of the entry's canonical form.
func HashOf(e Entry) (string, error) {
	c, err := canonical(e)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(c)
	return hex.EncodeToString(sum[:]), nil
}

// SHA256Hex is the hex SHA-256 of a body.
func SHA256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// Chain appends entries to one file. One Chain per path; Append is safe for concurrent use.
type Chain struct {
	mu   sync.Mutex
	path string
	seq  int
	last string
}

var (
	chainsMu sync.Mutex
	chains   = map[string]*Chain{}
)

// ChainAt returns the chain for a path, resuming from the file's last entry if it exists.
func ChainAt(path string) (*Chain, error) {
	chainsMu.Lock()
	defer chainsMu.Unlock()
	if c, ok := chains[path]; ok {
		return c, nil
	}
	c := &Chain{path: path, last: Genesis}
	f, err := os.Open(path)
	if err == nil {
		defer f.Close()
		entries, verr := Verify(f)
		if verr != nil {
			return nil, fmt.Errorf("existing chain at %s does not verify, refusing to extend it: %w", path, verr)
		}
		if n := len(entries); n > 0 {
			c.seq, c.last = entries[n-1].Seq, entries[n-1].Hash
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	chains[path] = c
	return c, nil
}

// Append fills in Seq, Prev and Hash, writes the entry as one line and syncs it. Returns the completed entry.
func (c *Chain) Append(e Entry) (Entry, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e.Seq, e.Prev, e.Hash = c.seq+1, c.last, ""
	h, err := HashOf(e)
	if err != nil {
		return e, err
	}
	e.Hash = h
	line, err := json.Marshal(e)
	if err != nil {
		return e, err
	}
	f, err := os.OpenFile(c.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return e, err
	}
	defer f.Close()
	if _, err := f.Write(append(line, '\n')); err != nil {
		return e, err
	}
	if err := f.Sync(); err != nil {
		return e, err
	}
	c.seq, c.last = e.Seq, e.Hash
	return e, nil
}

// Verify reads a whole chain and checks every link. It returns the entries read before the first problem.
func Verify(r io.Reader) ([]Entry, error) {
	var out []Entry
	prev, seq := Genesis, 0
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	for line := 1; sc.Scan(); line++ {
		if len(bytes.TrimSpace(sc.Bytes())) == 0 {
			continue
		}
		var e Entry
		if err := json.Unmarshal(sc.Bytes(), &e); err != nil {
			return out, fmt.Errorf("line %d: not an entry: %w", line, err)
		}
		if e.Seq != seq+1 {
			return out, fmt.Errorf("line %d: seq %d, expected %d", line, e.Seq, seq+1)
		}
		if e.Prev != prev {
			return out, fmt.Errorf("line %d (seq %d): prev does not match the hash of the entry before", line, e.Seq)
		}
		want, err := HashOf(e)
		if err != nil {
			return out, err
		}
		if e.Hash != want {
			return out, fmt.Errorf("line %d (seq %d): hash does not match the entry's contents", line, e.Seq)
		}
		out = append(out, e)
		prev, seq = e.Hash, e.Seq
	}
	return out, sc.Err()
}
