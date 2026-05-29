package app

import (
	"context"
	"encoding/base64"

	"github.com/byte-v-forge/gpt/gopay/protocol"
)

type supportCustomerInitiateBody struct {
	SupportLang string `json:"support_lang"`
	SupportCode string `json:"support_code"`
	SupportID   string `json:"support_id"`
	Data        string `json:"data"`
}

func (c *Client) InitiateSupportCustomer(ctx context.Context) (*protocol.Response, error) {
	return c.Post(ctx, CustomerBaseURL+"/v1/support/customer/initiate", newSupportCustomerInitiateBody())
}

func newSupportCustomerInitiateBody() supportCustomerInitiateBody {
	return supportCustomerInitiateBody{
		SupportLang: randomHex(256),
		SupportCode: randomHex(256),
		SupportID:   randomHex(256),
		Data:        base64.StdEncoding.EncodeToString(randomBytes(564)),
	}
}
