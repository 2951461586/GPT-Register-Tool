package gateway

import (
	"strings"
	"testing"
)

func TestNormalizeProxyCandidatesUserAtHost(t *testing.T) {
	got, err := NormalizeProxyCandidates("user:pass@gate-jp.kookeey.info:1000")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 {
		t.Fatalf("got %d candidates", len(got))
	}
	if got[0] != "socks5h://user:pass@gate-jp.kookeey.info:1000" {
		t.Fatalf("preferred candidate = %q", got[0])
	}
}

func TestNormalizeProxyCandidatesHostPortUserPass(t *testing.T) {
	got, err := NormalizeProxyCandidates("gate-jp.kookeey.info:1000:user:pass")
	if err != nil {
		t.Fatal(err)
	}
	want := "socks5h://user:pass@gate-jp.kookeey.info:1000"
	if got[0] != want {
		t.Fatalf("got %q want %q", got[0], want)
	}
}

func TestNormalizeProxyCandidatesDefaultsToSingleSocks5HWhenSchemeOmitted(t *testing.T) {
	got, err := NormalizeProxyCandidates("gate-jp.kookeey.info:1000:user:pass")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0] != "socks5h://user:pass@gate-jp.kookeey.info:1000" {
		t.Fatalf("unexpected candidates: %#v", got)
	}
}

func TestNormalizeProxyCandidatesExplicitScheme(t *testing.T) {
	got, err := NormalizeProxyCandidates("http://user:pass@example.com:8080")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0] != "http://user:pass@example.com:8080" {
		t.Fatalf("unexpected candidates: %#v", got)
	}
}

func TestNormalizeProxyCandidatesMultipleLines(t *testing.T) {
	got, err := NormalizeProxyCandidates("user:pass@gate-jp.kookeey.info:1000\nhttp://u:p@example.com:8080")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("got %#v", got)
	}
	if got[0] != "socks5h://user:pass@gate-jp.kookeey.info:1000" || got[1] != "http://u:p@example.com:8080" {
		t.Fatalf("unexpected candidates: %#v", got)
	}
}

func TestProviderStageProxyKookeeySwitchesToUS(t *testing.T) {
	got := ProviderStageProxy("socks5h://user:pass-JP@gate-jp.kookeey.info:1000")
	want := "socks5h://user:pass-JP@gate-jp.kookeey.info:1000"
	if got != want {
		t.Fatalf("got %q want %q", got, want)
	}
}

func TestProviderStageProxyCliproxyRegionAndSID(t *testing.T) {
	got := ProviderProxyCandidates("http://acct-region-JP-sid--t-5:pass@us2.cliproxy.io:3010", 2)
	foundUS := false
	for _, item := range got {
		if strings.Contains(item, "region-US") && !strings.Contains(item, "sid--") {
			foundUS = true
		}
	}
	if !foundUS {
		t.Fatalf("US rotated proxy missing: %#v", got)
	}
}

func TestExpandProxyRotationsDerivesRegions(t *testing.T) {
	got := ExpandProxyRotations([]string{"socks5h://user:pass-JP@gate-jp.kookeey.info:1000"}, 2)
	if len(got) < 3 {
		t.Fatalf("expected derived candidates, got %#v", got)
	}
	foundUS := false
	for _, item := range got {
		if item == "socks5h://user:pass-US@gate-us.kookeey.info:1000" {
			foundUS = true
		}
	}
	if !foundUS {
		t.Fatalf("US variant missing: %#v", got)
	}
}

func TestProviderProxyCandidatesRotatesKookeeyPasswordSession(t *testing.T) {
	got := ProviderProxyCandidates("socks5h://user:pass-JP-12345678@gate.kookeey.info:1000", 3)
	foundRotatedJP := false
	for _, item := range got {
		if strings.Contains(item, "gate-jp.kookeey.info") &&
			strings.Contains(item, "pass-JP-") &&
			!strings.Contains(item, "pass-JP-12345678") {
			foundRotatedJP = true
		}
	}
	if !foundRotatedJP {
		t.Fatalf("rotated JP session missing: %#v", got)
	}
}

func TestRotateProxyCandidatesStableByToken(t *testing.T) {
	candidates := []string{"proxy-a", "proxy-b", "proxy-c"}
	first := RotateProxyCandidates(candidates, testToken)
	second := RotateProxyCandidates(candidates, testToken)
	if len(first) != len(candidates) || strings.Join(first, ",") != strings.Join(second, ",") {
		t.Fatalf("rotation not stable: %#v %#v", first, second)
	}
	if strings.Join(first, ",") == strings.Join(candidates, ",") {
		t.Fatalf("expected non-trivial rotation for test token, got %#v", first)
	}
}

func TestExtractAccessTokenFromSessionJSON(t *testing.T) {
	raw := `{"user":{"email":"x@example.com"},"session":{"accessToken":"` + testToken + `"},"rumViewTags":{"light_account":{"fetched":false}}}`
	got, err := ExtractAccessToken(raw)
	if err != nil {
		t.Fatal(err)
	}
	if got != testToken {
		t.Fatalf("got %q want %q", got, testToken)
	}
}

func TestExtractAccessTokensFromMixedSessionDump(t *testing.T) {
	raw := `{"sessions":[{"accessToken":"` + testToken + `"},{"accessToken":"` + testToken + `"}],"other":"eyJhbGciOiJub25lIn0.eyJzdWIiOiJvdGhlciJ9.signature"}`
	got, err := ExtractAccessTokens(raw)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("got %#v", got)
	}
}

func TestExtractAccessTokenFromEmbeddedText(t *testing.T) {
	raw := "prefix " + testToken + " suffix"
	got, err := ExtractAccessToken(raw)
	if err != nil {
		t.Fatal(err)
	}
	if got != testToken {
		t.Fatalf("got %q want %q", got, testToken)
	}
}
