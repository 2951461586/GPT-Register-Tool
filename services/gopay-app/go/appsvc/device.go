package appsvc

import (
	"strings"

	gopayapp "github.com/byte-v-forge/gpt/gopay/protocol/app"
)

func deviceFromMap(raw map[string]any) gopayapp.DeviceFingerprint {
	get := func(keys ...string) string {
		for _, key := range keys {
			if value := anyString(raw[key]); value != "" {
				return value
			}
		}
		return ""
	}
	return normalizeDeviceShape(gopayapp.DeviceFingerprint{
		AppType:          get("x-apptype", "AppType"),
		AppVersion:       get("x-appversion", "AppVersion"),
		AppID:            get("x-appid", "AppID"),
		Platform:         get("x-platform", "Platform"),
		UniqueID:         get("x-uniqueid", "UniqueID"),
		PhoneMake:        get("x-phonemake", "PhoneMake"),
		PhoneModel:       get("x-phonemodel", "PhoneModel"),
		DeviceOS:         get("x-deviceos", "DeviceOS"),
		UserType:         get("x-user-type", "UserType"),
		SessionID:        get("x-session-id", "SessionID"),
		TransactionID:    get("transaction-id", "TransactionID"),
		UserAgent:        get("user-agent", "UserAgent"),
		D1:               get("d1", "D1"),
		XE2:              get("x-e2", "XE2"),
		AdjTS:            get("adjts", "AdjTS"),
		AppsFlyerID:      get("m1_appsflyer_id", "AppsFlyerID"),
		WidevineID:       get("m1_widevine_id", "WidevineID"),
		Screen:           get("m1_screen", "Screen"),
		WiFiMAC:          get("m1_wifi_mac", "WiFiMAC"),
		WiFiSSID:         get("m1_wifi_ssid", "WiFiSSID"),
		M1ConnectionID:   get("m1_connection_id", "M1ConnectionID"),
		M1Hardware:       get("m1_hardware", "m1_device_hardware", "M1Hardware"),
		M1Signature:      get("m1_signature", "M1Signature"),
		M1SignatureTime:  get("m1_signature_time", "M1SignatureTime"),
		M1DeviceUUID:     get("m1_device_uuid", "M1DeviceUUID"),
		FirebaseID:       get("m1_firebase_app_instance_id", "firebase_app_instance_id", "FirebaseID"),
		AdvertisingID:    get("advertising_id", "ad_id", "AdvertisingID"),
		AppSetID:         get("app_set_id", "AppSetID"),
		InstallReferrer:  get("install_referrer", "InstallReferrer"),
		InstallerPackage: get("installer_package", "InstallerPackage"),
		GMSVersion:       get("gms_version", "play_services_version", "GMSVersion"),
		UserUUID:         get("user-uuid", "UserUUID"),
		DeviceToken:      get("x-devicetoken", "DeviceToken"),
		IMEI:             get("x-imei", "IMEI"),
		IPAddress:        get("x-ipaddress", "x-ip-address", "IPAddress"),
		Location:         get("x-location", "Location"),
		LocationAccuracy: get("x-location-accuracy", "LocationAccuracy"),
		GojekCountryCode: get("gojek-country-code", "GojekCountryCode"),
		TLSProfileName:   get("tls_profile", "tls-profile", "TLSProfileName"),
	})
}

func deviceToMap(device gopayapp.DeviceFingerprint) map[string]any {
	return map[string]any{
		"x-apptype":                   device.AppType,
		"x-appversion":                device.AppVersion,
		"x-appid":                     device.AppID,
		"x-platform":                  device.Platform,
		"x-uniqueid":                  device.UniqueID,
		"x-phonemake":                 device.PhoneMake,
		"x-phonemodel":                device.PhoneModel,
		"x-deviceos":                  device.DeviceOS,
		"x-user-type":                 device.UserType,
		"x-session-id":                device.SessionID,
		"transaction-id":              device.TransactionID,
		"user-agent":                  device.UserAgent,
		"d1":                          device.D1,
		"x-e2":                        device.XE2,
		"adjts":                       device.AdjTS,
		"m1_appsflyer_id":             device.AppsFlyerID,
		"m1_widevine_id":              device.WidevineID,
		"m1_screen":                   device.Screen,
		"m1_wifi_mac":                 device.WiFiMAC,
		"m1_wifi_ssid":                device.WiFiSSID,
		"m1_connection_id":            device.M1ConnectionID,
		"m1_hardware":                 device.M1Hardware,
		"m1_signature":                device.M1Signature,
		"m1_signature_time":           device.M1SignatureTime,
		"m1_device_uuid":              device.M1DeviceUUID,
		"m1_firebase_app_instance_id": device.FirebaseID,
		"advertising_id":              device.AdvertisingID,
		"app_set_id":                  device.AppSetID,
		"install_referrer":            device.InstallReferrer,
		"installer_package":           device.InstallerPackage,
		"gms_version":                 device.GMSVersion,
		"user-uuid":                   device.UserUUID,
		"x-devicetoken":               device.DeviceToken,
		"x-imei":                      device.IMEI,
		"x-ipaddress":                 device.IPAddress,
		"x-location":                  device.Location,
		"x-location-accuracy":         device.LocationAccuracy,
		"gojek-country-code":          device.GojekCountryCode,
		"tls_profile":                 device.TLSProfileName,
	}
}

func mergeDevice(current, fallback gopayapp.DeviceFingerprint) gopayapp.DeviceFingerprint {
	if current.AppType == "" {
		current.AppType = fallback.AppType
	}
	if current.AppVersion == "" {
		current.AppVersion = fallback.AppVersion
	}
	if current.AppID == "" {
		current.AppID = fallback.AppID
	}
	if current.Platform == "" {
		current.Platform = fallback.Platform
	}
	if current.UniqueID == "" {
		current.UniqueID = fallback.UniqueID
	}
	if current.PhoneMake == "" {
		current.PhoneMake = fallback.PhoneMake
	}
	if current.PhoneModel == "" {
		current.PhoneModel = fallback.PhoneModel
	}
	if current.DeviceOS == "" {
		current.DeviceOS = fallback.DeviceOS
	}
	if current.UserType == "" {
		current.UserType = fallback.UserType
	}
	if current.SessionID == "" {
		current.SessionID = fallback.SessionID
	}
	if current.TransactionID == "" {
		current.TransactionID = fallback.TransactionID
	}
	if current.UserAgent == "" {
		current.UserAgent = fallback.UserAgent
	}
	if current.D1 == "" {
		current.D1 = fallback.D1
	}
	if current.AdjTS == "" {
		current.AdjTS = fallback.AdjTS
	}
	if current.AppsFlyerID == "" {
		current.AppsFlyerID = fallback.AppsFlyerID
	}
	if current.WidevineID == "" {
		current.WidevineID = fallback.WidevineID
	}
	if current.Screen == "" {
		current.Screen = fallback.Screen
	}
	if current.WiFiMAC == "" {
		current.WiFiMAC = fallback.WiFiMAC
	}
	if current.WiFiSSID == "" {
		current.WiFiSSID = fallback.WiFiSSID
	}
	if current.M1ConnectionID == "" {
		current.M1ConnectionID = fallback.M1ConnectionID
	}
	if current.M1Hardware == "" {
		current.M1Hardware = fallback.M1Hardware
	}
	if current.M1Signature == "" {
		current.M1Signature = fallback.M1Signature
	}
	if current.M1SignatureTime == "" {
		current.M1SignatureTime = fallback.M1SignatureTime
	}
	if current.M1DeviceUUID == "" {
		current.M1DeviceUUID = fallback.M1DeviceUUID
	}
	if current.FirebaseID == "" {
		current.FirebaseID = fallback.FirebaseID
	}
	if current.AdvertisingID == "" {
		current.AdvertisingID = fallback.AdvertisingID
	}
	if current.AppSetID == "" {
		current.AppSetID = fallback.AppSetID
	}
	if current.InstallReferrer == "" {
		current.InstallReferrer = fallback.InstallReferrer
	}
	if current.InstallerPackage == "" {
		current.InstallerPackage = fallback.InstallerPackage
	}
	if current.GMSVersion == "" {
		current.GMSVersion = fallback.GMSVersion
	}
	if current.DeviceToken == "" {
		current.DeviceToken = fallback.DeviceToken
	}
	if current.IMEI == "" {
		current.IMEI = fallback.IMEI
	}
	if current.IPAddress == "" {
		current.IPAddress = fallback.IPAddress
	}
	if current.Location == "" {
		current.Location = fallback.Location
	}
	if current.LocationAccuracy == "" {
		current.LocationAccuracy = fallback.LocationAccuracy
	}
	if current.GojekCountryCode == "" {
		current.GojekCountryCode = fallback.GojekCountryCode
	}
	if current.TLSProfileName == "" {
		current.TLSProfileName = fallback.TLSProfileName
	}
	return normalizeDeviceShape(current)
}

func normalizeDeviceShape(device gopayapp.DeviceFingerprint) gopayapp.DeviceFingerprint {
	device.PhoneModel = normalizeCommaValue(device.PhoneModel)
	device.DeviceOS = normalizeAndroidOSValue(device.DeviceOS)
	return device
}

func normalizeCommaValue(value string) string {
	value = strings.TrimSpace(value)
	parts := strings.SplitN(value, ",", 2)
	if len(parts) != 2 {
		return value
	}
	return strings.TrimSpace(parts[0]) + ", " + strings.TrimSpace(parts[1])
}

func normalizeAndroidOSValue(value string) string {
	value = strings.TrimSpace(value)
	if value == "" || !strings.HasPrefix(strings.ToLower(value), "android") {
		return value
	}
	return normalizeCommaValue(value)
}
