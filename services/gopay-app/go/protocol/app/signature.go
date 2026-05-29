package app

import (
	"bytes"
	"crypto/hmac"
	"crypto/md5"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/url"
	"os"
	"strings"
	"time"
)

const emptyBodyMD5 = "d41d8cd98f00b204e9800998ecf8427e"

const (
	defaultGoPaySignVersion             = "auto"
	defaultGoPayDisplayEncoderID        = "D"
	defaultGoPayDisplayEncoderKey       = "1V79g&FZMB#zQ9:[T+8*xr1FXYVJ#%J)LiKl?c?=JG8dc{cX?d?p-u&Ti)$<vJC"
	defaultGoPayLegacyDisplayEncoderKey = "4&G6DbV&j8QZs~{)(Ila_w_|v@aqJq]E-;*(J9PanZ8sm01kTi{X<iG``]d7P&L"
	goPayV2TailConst                    = "c244dc56c7b6026a"
)

type Signer struct {
	Now                   func() time.Time
	SignVersion           string
	LegacyHMACKey         string
	DisplayEncoderKey     string
	DisplayEncoderID      string
	SignedMsgTemplatePath string
}

type Signature struct {
	XE1     string
	BodyMD5 string
}

func (s Signer) Sign(method string, rawURL string, body []byte, token string, device DeviceFingerprint, xM1 string) (Signature, error) {
	bodyMD5 := emptyBodyMD5
	if len(body) > 0 {
		sum := md5.Sum(body)
		bodyMD5 = hex.EncodeToString(sum[:])
	}
	now := time.Now()
	if s.Now != nil {
		now = s.Now()
	}
	timestamp := fmt.Sprint(now.UnixMilli())
	version := s.signVersionForRequest(rawURL)
	if version == "v2" {
		return s.signV2(method, rawURL, token, device, xM1, timestamp, bodyMD5)
	}
	return s.signLegacy(method, rawURL, token, device, xM1, timestamp, bodyMD5)
}

func (s Signer) signVersionForRequest(rawURL string) string {
	configured := strings.ToLower(strings.TrimSpace(s.SignVersion))
	switch configured {
	case "v1", "legacy":
		return "v1"
	case "v2":
		return "v2"
	}
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return "v1"
	}
	host := strings.ToLower(parsed.Host)
	path := parsed.Path
	if host == "customer.gopayapi.com" {
		switch {
		case path == "/api/v1/users/pin/tokens/nb":
			return "v2"
		case path == "/v1/support/customer/activity":
			return "v2"
		}
	}
	return "v1"
}

func (s Signer) signLegacy(method string, rawURL string, token string, device DeviceFingerprint, xM1 string, timestamp string, bodyMD5 string) (Signature, error) {
	field1, err := randomField1()
	if err != nil {
		return Signature{}, err
	}
	path := signaturePath(rawURL)
	jwt := strings.TrimPrefix(token, "Bearer ")
	if xM1 == "" {
		xM1 = device.XM1()
	}
	parts := []string{
		device.AppType,
		device.PhoneModel + ":" + jwt,
		device.UniqueID + ":",
		bodyMD5 + ":" + path,
		strings.ToUpper(method) + ":" + timestamp,
		device.DeviceOS + ":" + device.AppVersion,
		xM1 + ":" + device.AppID,
		field1 + ":" + device.PhoneMake,
		device.Platform,
	}
	msg := strings.Join(parts, ";")
	mac := hmac.New(sha256.New, []byte(firstNonEmpty(s.LegacyHMACKey, defaultGoPayLegacyDisplayEncoderKey)))
	_, _ = mac.Write([]byte(msg))
	return Signature{
		XE1:     hex.EncodeToString(mac.Sum(nil)) + ":" + field1 + ":D:" + timestamp,
		BodyMD5: bodyMD5,
	}, nil
}

func (s Signer) signV2(method string, rawURL string, token string, device DeviceFingerprint, xM1 string, timestamp string, bodyMD5 string) (Signature, error) {
	nonce, err := randomAlnum(32)
	if err != nil {
		return Signature{}, err
	}
	cipherHex, t3First16Hex := goPayV2Cipher(nonce)
	realMsg := s.goPayV2RealMsg(method, rawURL, token, device, xM1, timestamp, bodyMD5, cipherHex, t3First16Hex)
	shaHex := hmacSHA256Hex([]byte(firstNonEmpty(s.DisplayEncoderKey, defaultGoPayDisplayEncoderKey)), realMsg)
	return Signature{
		XE1:     shaHex + ":" + cipherHex + ":" + firstNonEmpty(s.DisplayEncoderID, defaultGoPayDisplayEncoderID) + ":" + timestamp,
		BodyMD5: bodyMD5,
	}, nil
}

func (s Signer) goPayV2RealMsg(method string, rawURL string, token string, device DeviceFingerprint, xM1 string, timestamp string, bodyMD5 string, cipherHex string, t3First16Hex string) []byte {
	key := []byte(firstNonEmpty(s.DisplayEncoderKey, defaultGoPayDisplayEncoderKey))
	msg := goPayV2SyntheticRealMsg(method, rawURL, token, device, xM1, timestamp, bodyMD5, cipherHex, t3First16Hex, key)
	if path := strings.TrimSpace(s.SignedMsgTemplatePath); path != "" {
		if templ, err := os.ReadFile(path); err == nil && len(templ) > 0 {
			if patched := patchGoPayV2Template(templ, msg, token, cipherHex, t3First16Hex); len(patched) > 0 {
				return patched
			}
		}
	}
	return msg
}

func goPayV2SyntheticRealMsg(method string, rawURL string, token string, device DeviceFingerprint, xM1 string, timestamp string, bodyMD5 string, cipherHex string, t3First16Hex string, key []byte) []byte {
	jwt := strings.TrimPrefix(strings.TrimSpace(token), "Bearer ")
	if xM1 == "" {
		xM1 = device.XM1()
	}
	var msg bytes.Buffer
	msg.Write(hmacInnerPad(key))
	msg.WriteString(jwt)
	msg.WriteByte(':')
	msg.WriteString(firstNonEmpty(device.PhoneModel, device.PhoneMake+", SM-G780F"))
	msg.WriteByte(':')
	msg.WriteString(firstNonEmpty(xM1, device.XM1()))
	msg.WriteByte(':')
	msg.WriteString(device.AppVersion)
	msg.WriteByte(':')
	msg.WriteString(firstNonEmpty(bodyMD5, emptyBodyMD5))
	msg.WriteByte(':')
	msg.WriteString(device.UniqueID)
	msg.WriteByte(':')
	msg.WriteString(strings.ToUpper(method))
	msg.WriteByte(':')
	msg.WriteString(device.DeviceOS)
	msg.WriteByte(':')
	msg.WriteString(timestamp)
	msg.WriteString("::")
	msg.WriteString(signaturePath(rawURL))
	msg.WriteByte(':')
	msg.WriteString(device.AppID)
	msg.WriteByte(':')
	msg.WriteString(cipherHex)
	msg.WriteString("0000000000000000")
	msg.WriteString("0000000000000000")
	msg.WriteString(goPayV2TailConst)
	msg.WriteString("0000000000000000")
	msg.WriteString(t3First16Hex)
	return msg.Bytes()
}

func patchGoPayV2Template(template []byte, fallback []byte, token string, cipherHex string, t3First16Hex string) []byte {
	patched := append([]byte(nil), template...)
	jwt := strings.TrimPrefix(strings.TrimSpace(token), "Bearer ")
	if jwt != "" {
		start := bytes.Index(patched, []byte("eyJhbGciOiJkaXIi"))
		end := bytes.Index(patched, []byte(":samsung,"))
		if start >= 0 && end > start {
			patched = append(append(append([]byte(nil), patched[:start]...), []byte(jwt)...), patched[end:]...)
		}
	}
	if len(patched) == 0 {
		return fallback
	}
	if idx := bytes.LastIndex(patched, []byte(goPayV2TailConst)); idx > 64 {
		searchStart := idx - 128
		if searchStart < 0 {
			searchStart = 0
		}
		window := patched[searchStart:idx]
		if old := lastHexRun(window, 64); old != "" {
			abs := searchStart + bytes.LastIndex(window, []byte(old))
			copy(patched[abs:abs+64], []byte(cipherHex))
		}
	}
	if len(t3First16Hex) == 32 && len(patched) >= 32 {
		copy(patched[len(patched)-32:], []byte(t3First16Hex))
	}
	return patched
}

func goPayV2Cipher(nonce string) (string, string) {
	zeroKey := make([]byte, 64)
	hkdfData := bytes.Repeat([]byte{1}, 64)
	expandTag := bytes.Repeat([]byte{1}, 32)
	keyC := hmacSHA256(zeroKey, hkdfData)
	keyD := hmacSHA256(keyC, hkdfData)
	k9Input := append(append(append(append([]byte{}, keyD...), expandTag...), keyC...), []byte(nonce)...)
	k9 := hmacSHA256(keyC, k9Input)
	t1 := hmacSHA256(k9, append(append([]byte{}, keyD...), expandTag...))
	t2 := hmacSHA256(k9, append(append([]byte{}, t1...), expandTag...))
	t3 := hmacSHA256(k9, append(append([]byte{}, t2...), expandTag...))
	return hex.EncodeToString(t2), hex.EncodeToString(t3[:16])
}

func hmacSHA256(key []byte, data []byte) []byte {
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write(data)
	return mac.Sum(nil)
}

func hmacSHA256Hex(key []byte, data []byte) string {
	return hex.EncodeToString(hmacSHA256(key, data))
}

func hmacInnerPad(key []byte) []byte {
	block := make([]byte, sha256.BlockSize)
	if len(key) > sha256.BlockSize {
		sum := sha256.Sum256(key)
		key = sum[:]
	}
	copy(block, key)
	for idx := range block {
		block[idx] ^= 0x36
	}
	return block
}

func signaturePath(rawURL string) string {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Host == "" {
		return strings.TrimPrefix(strings.TrimPrefix(rawURL, "https://"), "http://")
	}
	return parsed.Host + parsed.RequestURI()
}

func randomAlnum(size int) (string, error) {
	const alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
	raw := make([]byte, size)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	out := make([]byte, size)
	for idx, value := range raw {
		out[idx] = alphabet[int(value)%len(alphabet)]
	}
	return string(out), nil
}

func randomField1() (string, error) {
	first := make([]byte, 32)
	middleA := make([]byte, 2)
	middleB := make([]byte, 4)
	second := make([]byte, 16)
	if _, err := rand.Read(first); err != nil {
		return "", err
	}
	if _, err := rand.Read(middleA); err != nil {
		return "", err
	}
	if _, err := rand.Read(middleB); err != nil {
		return "", err
	}
	if _, err := rand.Read(second); err != nil {
		return "", err
	}
	middle := "2000000040000000" +
		hex.EncodeToString(middleA) + "cf0f" +
		"28e4f5be08e4f5be" +
		hex.EncodeToString(middleB) +
		"c8e3f5befb1aad58"
	return hex.EncodeToString(first) + middle + hex.EncodeToString(second), nil
}

func lastHexRun(value []byte, size int) string {
	for idx := len(value) - size; idx >= 0; idx-- {
		candidate := value[idx : idx+size]
		if isHexASCII(candidate) {
			return string(candidate)
		}
	}
	return ""
}

func isHexASCII(value []byte) bool {
	for _, ch := range value {
		if (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f') || (ch >= 'A' && ch <= 'F') {
			continue
		}
		return false
	}
	return true
}
