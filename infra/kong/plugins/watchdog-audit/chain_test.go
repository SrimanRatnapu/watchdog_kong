package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func fresh(t *testing.T) (*Chain, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "chain.jsonl")
	chainsMu.Lock()
	delete(chains, path)
	chainsMu.Unlock()
	c, err := ChainAt(path)
	if err != nil {
		t.Fatal(err)
	}
	return c, path
}

func entry(caseID string, status int) Entry {
	return Entry{At: "2026-10-25T00:00:00Z", Consumer: "agents", CaseID: caseID, Model: "claude-sonnet-5-5",
		Status: status, Source: "service", RequestSHA256: SHA256Hex([]byte("req")), ResponseSHA256: SHA256Hex([]byte("resp"))}
}

func readAll(t *testing.T, path string) []Entry {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	es, err := Verify(f)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	return es
}

func TestAppendChainsEntries(t *testing.T) {
	c, path := fresh(t)
	for i := 0; i < 3; i++ {
		if _, err := c.Append(entry("C-0001", 200)); err != nil {
			t.Fatal(err)
		}
	}
	es := readAll(t, path)
	if len(es) != 3 || es[0].Prev != Genesis || es[1].Prev != es[0].Hash || es[2].Seq != 3 {
		t.Fatalf("bad chain: %+v", es)
	}
}

func TestEditingAnyEntryIsDetected(t *testing.T) {
	c, path := fresh(t)
	for i := 0; i < 4; i++ {
		c.Append(entry("C-0002", 200))
	}
	raw, _ := os.ReadFile(path)
	lines := strings.Split(strings.TrimSpace(string(raw)), "\n")

	cases := map[string]func([]string) []string{
		"field changed": func(l []string) []string {
			l[1] = strings.Replace(l[1], `"status":200`, `"status":400`, 1)
			return l
		},
		"entry removed":   func(l []string) []string { return append(l[:1:1], l[2:]...) },
		"entries swapped": func(l []string) []string { l[1], l[2] = l[2], l[1]; return l },
		"hash recomputed": func(l []string) []string { return rehashOne(t, l, 1) }, // fixes its own hash, not the next prev
	}
	for name, edit := range cases {
		t.Run(name, func(t *testing.T) {
			edited := edit(append([]string(nil), lines...))
			if _, err := Verify(strings.NewReader(strings.Join(edited, "\n"))); err == nil {
				t.Fatalf("%s was not detected", name)
			}
		})
	}
}

func rehashOne(t *testing.T, lines []string, i int) []string {
	var e Entry
	if err := json.Unmarshal([]byte(lines[i]), &e); err != nil {
		t.Fatal(err)
	}
	e.CaseID = "C-9999"
	e.Hash, _ = HashOf(e)
	b, _ := json.Marshal(e)
	lines[i] = string(b)
	return lines
}

func TestResumeContinuesTheChainAcrossRestarts(t *testing.T) {
	c, path := fresh(t)
	c.Append(entry("C-0003", 200))
	chainsMu.Lock()
	delete(chains, path) // as if the plugin server restarted
	chainsMu.Unlock()
	c2, err := ChainAt(path)
	if err != nil {
		t.Fatal(err)
	}
	c2.Append(entry("C-0003", 400))
	if es := readAll(t, path); len(es) != 2 || es[1].Prev != es[0].Hash {
		t.Fatalf("chain broke across restart: %+v", es)
	}
}

func TestRefusesToExtendABrokenChain(t *testing.T) {
	_, path := fresh(t)
	os.WriteFile(path, []byte(`{"seq":1,"prev":"x","hash":"y"}`+"\n"), 0o644)
	chainsMu.Lock()
	delete(chains, path)
	chainsMu.Unlock()
	if _, err := ChainAt(path); err == nil {
		t.Fatal("extended a chain that does not verify")
	}
}

func TestConcurrentAppendsStayOrdered(t *testing.T) {
	c, path := fresh(t)
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); c.Append(entry("C-0004", 200)) }()
	}
	wg.Wait()
	if es := readAll(t, path); len(es) != 50 {
		t.Fatalf("got %d entries", len(es))
	}
}

func TestCanonicalFormMatchesThePythonVerifier(t *testing.T) {
	// scripts/verify_audit_chain.py hashes json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	// of the entry without "hash". Pin the exact bytes so both sides stay in step.
	e := entry("C-<&>-ñ", 200)
	e.Seq, e.Prev = 1, Genesis
	got, _ := canonical(e)
	want := `{"at":"2026-10-25T00:00:00Z","blocked":false,"case_id":"C-<&>-ñ","consumer":"agents","model":"claude-sonnet-5-5",` +
		`"prev":"` + Genesis + `","request_sha256":"` + SHA256Hex([]byte("req")) + `","response_sha256":"` +
		SHA256Hex([]byte("resp")) + `","seq":1,"source":"service","status":200}`
	if !bytes.Equal(got, []byte(want)) {
		t.Fatalf("canonical form changed:\n got %s\nwant %s", got, want)
	}
}

// The same entry and hash are pinned in backend/tests/test_audit_chain.py, so Go and Python agree byte for byte.
const pinnedHash = "e6744b669696d770f8c49184552a26d051c6f4f1cbde7b3ac6a5e5c635fd3868"

func TestHashMatchesThePythonVerifier(t *testing.T) {
	e := entry("C-<&>-ñ", 200)
	e.Seq, e.Prev = 1, Genesis
	if h, _ := HashOf(e); h != pinnedHash {
		t.Fatalf("hash %s, Python computes %s", h, pinnedHash)
	}
}
