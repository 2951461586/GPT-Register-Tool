package app

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"math/big"
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/byte-v-forge/gpt/gopay/protocol"
)

const (
	defaultAppVersion      = "2.7.0"
	defaultAppID           = "com.gojek.gopay"
	defaultAppBuild        = "2070"
	defaultGojekCountry    = "ID"
	defaultAuthSDKVersion  = "1.0.0"
	defaultCVSDKVersion    = "1.0.0"
	defaultSupportSDK      = "0.44.0"
	defaultAcceptLanguage  = "en-ID"
	defaultTimezone        = "Asia/Jakarta"
	defaultUserLocale      = "en_ID"
	defaultAndroidVersion  = "7.0"
	defaultXE2             = "ED9A2B38749FBDE9ACA61D6A685B7"
	defaultPhoneMake       = "HUAWEI"
	defaultPhoneModel      = "HUAWEI, TRT-AL00A"
	defaultUniqueID        = "685b86605a047a3e"
	defaultD1              = "00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00"
	defaultAppsFlyerID     = "1779516675040-8955649077185556133"
	defaultWidevineID      = "T1B0eHZMQmFWV0h2UlBRSllIeVdlbFNtS1BqcXFiZwA="
	defaultXM1ConnectionID = "55093"
	defaultXM1Screen       = "720x1208"
	defaultXM1WiFiMAC      = "6c:b1:58:31:29:5b"
	defaultXM1WiFiSSID     = "Bug"
	defaultXM1Hardware     = "msm8937|1401|8"
	defaultM1Signature     = "0000000000000000"
	defaultM1DeviceUUID    = "00000000-0000-0000-0000-000000000000"
	defaultFirebaseID      = "00000000000000000000000000000000"
	defaultAdvertisingID   = "00000000-0000-0000-0000-000000000000"
	defaultAppSetID        = "00000000-0000-0000-0000-000000000000"
	defaultInstallReferrer = "utm_source=google-play&utm_medium=organic"
	defaultInstaller       = "com.android.vending"
	defaultGMSVersion      = "252014000"
	defaultLocation        = "-6.2000000,106.8000000"
	defaultLocationAcc     = "0.010999999552965164"
	defaultPlatform        = "Android"
	defaultUserType        = "customer"
	defaultApplicationType = "GOPAY"
)

type hardwareProfile struct {
	AndroidVersion string
	PhoneMake      string
	PhoneModel     string
	Screen         string
}

var hardwareProfiles = []hardwareProfile{
	{AndroidVersion: defaultAndroidVersion, PhoneMake: defaultPhoneMake, PhoneModel: defaultPhoneModel, Screen: defaultXM1Screen},
	{AndroidVersion: "12", PhoneMake: "samsung", PhoneModel: "samsung,SM-A525F", Screen: "1080x2174"},
	{AndroidVersion: "13", PhoneMake: "samsung", PhoneModel: "samsung,SM-A536E", Screen: "1080x2176"},
	{AndroidVersion: "13", PhoneMake: "samsung", PhoneModel: "samsung,SM-M336B", Screen: "1080x2193"},
	{AndroidVersion: "16", PhoneMake: "Xiaomi", PhoneModel: "Redmi,23117RK66C", Screen: "1080x2400"},
	{AndroidVersion: "13", PhoneMake: "Xiaomi", PhoneModel: "Redmi,2201117TY", Screen: "1080x2177"},
	{AndroidVersion: "12", PhoneMake: "Xiaomi", PhoneModel: "Redmi,M2101K7BNY", Screen: "1080x2150"},
	{AndroidVersion: "13", PhoneMake: "OPPO", PhoneModel: "OPPO,CPH2385", Screen: "1080x2172"},
	{AndroidVersion: "12", PhoneMake: "vivo", PhoneModel: "vivo,V2111", Screen: "1080x2179"},
}

type DeviceConfig struct {
	StaticIdentity   bool
	AppVersion       string
	AppID            string
	AppBuild         string
	AndroidVersion   string
	PhoneMake        string
	PhoneModel       string
	UniqueID         string
	SessionID        string
	TransactionID    string
	UserAgent        string
	D1               string
	XE2              string
	AdjTS            string
	AppsFlyerID      string
	WidevineID       string
	Screen           string
	WiFiMAC          string
	WiFiSSID         string
	M1ConnectionID   string
	M1Hardware       string
	M1Signature      string
	M1SignatureTime  string
	M1DeviceUUID     string
	FirebaseID       string
	AdvertisingID    string
	AppSetID         string
	InstallReferrer  string
	InstallerPackage string
	GMSVersion       string
	UserUUID         string
	DeviceToken      string
	IMEI             string
	IPAddress        string
	Location         string
	LocationAccuracy string
	GojekCountryCode string
	TLSProfileName   string
}

func DeviceConfigFromEnv() DeviceConfig {
	return DeviceConfig{
		StaticIdentity:   envBool("GOPAY_STATIC_DEVICE_IDENTITY"),
		AppVersion:       getenv("GOPAY_APP_VERSION"),
		AppID:            getenv("GOPAY_APP_ID"),
		AppBuild:         getenv("GOPAY_APP_BUILD"),
		AndroidVersion:   getenv("GOPAY_ANDROID_VERSION"),
		PhoneMake:        getenv("GOPAY_PHONE_MAKE"),
		PhoneModel:       getenv("GOPAY_PHONE_MODEL"),
		UniqueID:         getenv("GOPAY_UNIQUE_ID"),
		SessionID:        getenv("GOPAY_SESSION_ID"),
		TransactionID:    getenv("GOPAY_TRANSACTION_ID"),
		UserAgent:        getenv("GOPAY_USER_AGENT"),
		D1:               getenv("GOPAY_D1"),
		XE2:              getenv("GOPAY_X_E2"),
		AdjTS:            getenv("GOPAY_ADJ_TS"),
		AppsFlyerID:      getenv("GOPAY_APPSFLYER_ID"),
		WidevineID:       getenv("GOPAY_WIDEVINE_ID"),
		Screen:           getenv("GOPAY_SCREEN"),
		WiFiMAC:          getenv("GOPAY_WIFI_MAC"),
		WiFiSSID:         getenv("GOPAY_WIFI_SSID"),
		M1ConnectionID:   getenv("GOPAY_M1_CONNECTION_ID"),
		M1Hardware:       firstNonEmpty(getenv("GOPAY_M1_HARDWARE"), getenv("GOPAY_M1_DEVICE_HARDWARE")),
		M1Signature:      getenv("GOPAY_M1_SIGNATURE"),
		M1SignatureTime:  getenv("GOPAY_M1_SIGNATURE_TIME"),
		M1DeviceUUID:     getenv("GOPAY_M1_DEVICE_UUID"),
		FirebaseID:       firstNonEmpty(getenv("GOPAY_FIREBASE_APP_INSTANCE_ID"), getenv("GOPAY_FIREBASE_ID")),
		AdvertisingID:    firstNonEmpty(getenv("GOPAY_ADVERTISING_ID"), getenv("GOPAY_AD_ID")),
		AppSetID:         getenv("GOPAY_APP_SET_ID"),
		InstallReferrer:  getenv("GOPAY_INSTALL_REFERRER"),
		InstallerPackage: getenv("GOPAY_INSTALLER_PACKAGE"),
		GMSVersion:       firstNonEmpty(getenv("GOPAY_GMS_VERSION"), getenv("GOPAY_PLAY_SERVICES_VERSION")),
		UserUUID:         getenv("GOPAY_USER_UUID"),
		DeviceToken:      getenv("GOPAY_DEVICE_TOKEN"),
		IMEI:             getenv("GOPAY_IMEI"),
		IPAddress:        firstNonEmpty(getenv("GOPAY_IP_ADDRESS"), getenv("GOPAY_LOCAL_IP_ADDRESS")),
		Location:         getenv("GOPAY_LOCATION"),
		LocationAccuracy: getenv("GOPAY_LOCATION_ACCURACY"),
		GojekCountryCode: getenv("GOPAY_COUNTRY_CODE"),
		TLSProfileName:   getenv("GOPAY_TLS_PROFILE"),
	}
}

type DeviceFingerprint struct {
	AppType          string
	AppVersion       string
	AppID            string
	Platform         string
	UniqueID         string
	PhoneMake        string
	PhoneModel       string
	DeviceOS         string
	UserType         string
	SessionID        string
	TransactionID    string
	UserAgent        string
	D1               string
	XE2              string
	AdjTS            string
	AppsFlyerID      string
	WidevineID       string
	Screen           string
	WiFiMAC          string
	WiFiSSID         string
	M1ConnectionID   string
	M1Hardware       string
	M1Signature      string
	M1SignatureTime  string
	M1DeviceUUID     string
	FirebaseID       string
	AdvertisingID    string
	AppSetID         string
	InstallReferrer  string
	InstallerPackage string
	GMSVersion       string
	UserUUID         string
	DeviceToken      string
	IMEI             string
	IPAddress        string
	Location         string
	LocationAccuracy string
	GojekCountryCode string
	TLSProfileName   string
}

func NewDeviceFingerprint(cfg DeviceConfig) (DeviceFingerprint, error) {
	profile := randomHardwareProfile(cfg.StaticIdentity)
	appVersion := firstNonEmpty(cfg.AppVersion, defaultAppVersion)
	appID := firstNonEmpty(cfg.AppID, defaultAppID)
	appBuild := firstNonEmpty(cfg.AppBuild, defaultAppBuild)
	deviceOS := androidDeviceOS(firstNonEmpty(cfg.AndroidVersion, profile.AndroidVersion))
	phoneMake := firstNonEmpty(cfg.PhoneMake, profile.PhoneMake)
	phoneModel := normalizePhoneModel(firstNonEmpty(cfg.PhoneModel, profile.PhoneModel))
	userAgent := firstNonEmpty(cfg.UserAgent, fmt.Sprintf("GoPay/%s (%s; build:%s; %s)", appVersion, appID, appBuild, deviceOS))
	uniqueID := firstNonEmpty(cfg.UniqueID, generatedOrStatic(cfg.StaticIdentity, defaultUniqueID, func() string { return randomHex(8) }))
	d1 := firstNonEmpty(cfg.D1, generatedOrStatic(cfg.StaticIdentity, defaultD1, randomD1))
	appsFlyerID := firstNonEmpty(cfg.AppsFlyerID, generatedOrStatic(cfg.StaticIdentity, defaultAppsFlyerID, randomAppsFlyerID))
	widevineID := firstNonEmpty(cfg.WidevineID, generatedOrStatic(cfg.StaticIdentity, defaultWidevineID, randomWidevineID))
	wifiMAC := strings.ToLower(firstNonEmpty(cfg.WiFiMAC, generatedOrStatic(cfg.StaticIdentity, defaultXM1WiFiMAC, randomWiFiMAC)))
	wifiSSID := firstNonEmpty(cfg.WiFiSSID, generatedOrStatic(cfg.StaticIdentity, defaultXM1WiFiSSID, randomWiFiSSID))
	m1ConnectionID := firstNonEmpty(cfg.M1ConnectionID, generatedOrStatic(cfg.StaticIdentity, defaultXM1ConnectionID, randomM1ConnectionID))
	m1Hardware := firstNonEmpty(cfg.M1Hardware, defaultXM1Hardware)
	m1Signature := firstNonEmpty(cfg.M1Signature, generatedOrStatic(cfg.StaticIdentity, defaultM1Signature, func() string { return randomHex(8) }))
	m1SignatureTime := firstNonEmpty(cfg.M1SignatureTime, generatedOrStatic(cfg.StaticIdentity, "0", randomM1SignatureTime))
	m1DeviceUUID := firstNonEmpty(cfg.M1DeviceUUID, generatedOrStatic(cfg.StaticIdentity, defaultM1DeviceUUID, uuid.NewString))
	firebaseID := firstNonEmpty(cfg.FirebaseID, generatedOrStatic(cfg.StaticIdentity, defaultFirebaseID, func() string { return randomHex(16) }))
	advertisingID := firstNonEmpty(cfg.AdvertisingID, generatedOrStatic(cfg.StaticIdentity, defaultAdvertisingID, uuid.NewString))
	appSetID := firstNonEmpty(cfg.AppSetID, generatedOrStatic(cfg.StaticIdentity, defaultAppSetID, uuid.NewString))
	deviceToken := strings.TrimSpace(cfg.DeviceToken)
	return DeviceFingerprint{
		AppType:          defaultApplicationType,
		AppVersion:       appVersion,
		AppID:            appID,
		Platform:         defaultPlatform,
		UniqueID:         uniqueID,
		PhoneMake:        phoneMake,
		PhoneModel:       phoneModel,
		DeviceOS:         deviceOS,
		UserType:         defaultUserType,
		SessionID:        firstNonEmpty(cfg.SessionID, uuid.NewString()),
		TransactionID:    firstNonEmpty(cfg.TransactionID, uuid.NewString()),
		UserAgent:        userAgent,
		D1:               d1,
		XE2:              firstNonEmpty(cfg.XE2, defaultXE2),
		AdjTS:            firstNonEmpty(cfg.AdjTS, "host:D"),
		AppsFlyerID:      appsFlyerID,
		WidevineID:       widevineID,
		Screen:           firstNonEmpty(cfg.Screen, profile.Screen),
		WiFiMAC:          wifiMAC,
		WiFiSSID:         wifiSSID,
		M1ConnectionID:   m1ConnectionID,
		M1Hardware:       m1Hardware,
		M1Signature:      m1Signature,
		M1SignatureTime:  m1SignatureTime,
		M1DeviceUUID:     m1DeviceUUID,
		FirebaseID:       firebaseID,
		AdvertisingID:    advertisingID,
		AppSetID:         appSetID,
		InstallReferrer:  firstNonEmpty(cfg.InstallReferrer, defaultInstallReferrer),
		InstallerPackage: firstNonEmpty(cfg.InstallerPackage, defaultInstaller),
		GMSVersion:       firstNonEmpty(cfg.GMSVersion, defaultGMSVersion),
		UserUUID:         strings.TrimSpace(cfg.UserUUID),
		DeviceToken:      deviceToken,
		IMEI:             firstNonEmpty(cfg.IMEI, uniqueID),
		IPAddress:        firstNonEmpty(cfg.IPAddress, generatedOrStatic(cfg.StaticIdentity, "", randomPrivateIP)),
		Location:         firstNonEmpty(cfg.Location, defaultLocation),
		LocationAccuracy: firstNonEmpty(cfg.LocationAccuracy, defaultLocationAcc),
		GojekCountryCode: firstNonEmpty(cfg.GojekCountryCode, defaultGojekCountry),
		TLSProfileName:   protocol.ResolveTLSProfileName(cfg.TLSProfileName),
	}, nil
}

func (d DeviceFingerprint) XM1() string {
	if d.usesGoPay27Profile() {
		return strings.Join([]string{
			"3:" + firstNonEmpty(d.AppsFlyerID, defaultAppsFlyerID),
			"4:" + firstNonEmpty(d.M1ConnectionID, defaultXM1ConnectionID),
			"5:" + firstNonEmpty(d.M1Hardware, defaultXM1Hardware),
			"6:" + firstNonEmpty(d.WiFiMAC, defaultXM1WiFiMAC),
			"7:" + firstNonEmpty(d.WiFiSSID, defaultXM1WiFiSSID),
			"8:" + firstNonEmpty(d.Screen, defaultXM1Screen),
			"10:0",
			"11:" + firstNonEmpty(d.WidevineID, defaultWidevineID),
			"15:" + firstNonEmpty(d.FirebaseID, defaultFirebaseID),
		}, ",")
	}
	return strings.Join([]string{
		"3:" + firstNonEmpty(d.AppsFlyerID, defaultAppsFlyerID),
		"4:" + firstNonEmpty(d.M1ConnectionID, defaultXM1ConnectionID),
		"5:" + firstNonEmpty(d.PhoneMake, defaultPhoneMake) + "|3200|2",
		"6:" + firstNonEmpty(d.WiFiMAC, defaultXM1WiFiMAC),
		"7:" + firstNonEmpty(d.WiFiSSID, defaultXM1WiFiSSID),
		"8:" + firstNonEmpty(d.Screen, defaultXM1Screen),
		"9:passive,network,fused,gps",
		"10:1",
		"11:" + firstNonEmpty(d.WidevineID, defaultWidevineID),
		"13:" + firstNonEmpty(d.M1Signature, defaultM1Signature),
		"14:" + firstNonEmpty(d.M1SignatureTime, "0"),
		"15:" + firstNonEmpty(d.FirebaseID, defaultFirebaseID),
		"16:" + firstNonEmpty(d.M1DeviceUUID, defaultM1DeviceUUID),
	}, ",")
}

func (d DeviceFingerprint) usesGoPay27Profile() bool {
	version := strings.TrimSpace(d.AppVersion)
	return version == "" || version == "2.7" || strings.HasPrefix(version, "2.7.")
}

func (d DeviceFingerprint) WithNewTransactionID() DeviceFingerprint {
	d.TransactionID = uuid.NewString()
	return d
}

func androidDeviceOS(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		value = defaultAndroidVersion
	}
	if strings.HasPrefix(strings.ToLower(value), "android") {
		parts := strings.SplitN(value, ",", 2)
		if len(parts) == 2 {
			return strings.TrimSpace(parts[0]) + ", " + strings.TrimSpace(parts[1])
		}
		return value
	}
	return "Android, " + value
}

func normalizePhoneModel(value string) string {
	value = strings.TrimSpace(value)
	parts := strings.SplitN(value, ",", 2)
	if len(parts) != 2 {
		return value
	}
	return strings.TrimSpace(parts[0]) + ", " + strings.TrimSpace(parts[1])
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func generatedOrStatic(static bool, staticValue string, generate func() string) string {
	if static {
		return staticValue
	}
	return generate()
}

func randomD1() string {
	raw := randomBytes(32)
	parts := make([]string, 0, len(raw))
	for _, b := range raw {
		parts = append(parts, fmt.Sprintf("%02X", b))
	}
	return strings.Join(parts, ":")
}

func randomHardwareProfile(static bool) hardwareProfile {
	if static || !envBool("GOPAY_RANDOM_HARDWARE_PROFILE") || len(hardwareProfiles) == 0 {
		return hardwareProfile{
			AndroidVersion: defaultAndroidVersion,
			PhoneMake:      defaultPhoneMake,
			PhoneModel:     defaultPhoneModel,
			Screen:         defaultXM1Screen,
		}
	}
	return hardwareProfiles[randomIntRange(0, len(hardwareProfiles)-1)]
}

func randomAppsFlyerID() string {
	installUnixMillis := time.Now().Add(-time.Duration(randomIntRange(60, 86_400*21)) * time.Second).UnixMilli()
	return fmt.Sprintf("%d-%09d%010d", installUnixMillis, randomIntRange(100_000_000, 999_999_999), randomIntRange(0, 9_999_999_999))
}

func randomWidevineID() string {
	return base64.StdEncoding.EncodeToString(randomBytes(32))
}

func randomWiFiMAC() string {
	raw := randomBytes(6)
	raw[0] = (raw[0] | 0x02) & 0xFE
	parts := make([]string, 0, len(raw))
	for _, b := range raw {
		parts = append(parts, fmt.Sprintf("%02x", b))
	}
	return strings.Join(parts, ":")
}

func randomWiFiSSID() string {
	return defaultXM1WiFiSSID
}

func randomM1ConnectionID() string {
	return fmt.Sprintf("%d", randomIntRange(10000, 99999))
}

func randomM1SignatureTime() string {
	seenAt := time.Now().Add(-time.Duration(randomIntRange(60, 86_400*7)) * time.Second)
	return fmt.Sprint(seenAt.UnixMilli())
}

func randomFCMToken() string {
	return randomURLSafe(11) + ":APA91b" + randomURLSafe(134)
}

func randomPrivateIP() string {
	switch randomIntRange(0, 2) {
	case 0:
		return fmt.Sprintf("192.168.%d.%d", randomIntRange(0, 50), randomIntRange(2, 254))
	case 1:
		return fmt.Sprintf("10.%d.%d.%d", randomIntRange(0, 50), randomIntRange(0, 255), randomIntRange(2, 254))
	default:
		return fmt.Sprintf("172.%d.%d.%d", randomIntRange(16, 31), randomIntRange(0, 255), randomIntRange(2, 254))
	}
}

func randomURLSafe(size int) string {
	const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
	raw := randomBytes(size)
	out := make([]byte, size)
	for idx, value := range raw {
		out[idx] = alphabet[int(value)%len(alphabet)]
	}
	return string(out)
}

func randomHex(size int) string {
	return hex.EncodeToString(randomBytes(size))
}

func randomBytes(size int) []byte {
	raw := make([]byte, size)
	if _, err := rand.Read(raw); err != nil {
		fallback := []byte(uuid.NewString())
		for i := range raw {
			raw[i] = fallback[i%len(fallback)]
		}
	}
	return raw
}

func randomIntRange(minValue int, maxValue int) int {
	if maxValue <= minValue {
		return minValue
	}
	n, err := rand.Int(rand.Reader, big.NewInt(int64(maxValue-minValue+1)))
	if err != nil {
		return minValue + int(time.Now().UnixNano()%int64(maxValue-minValue+1))
	}
	return minValue + int(n.Int64())
}
