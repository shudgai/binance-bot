#!/bin/bash
API_KEY="rmIKrfcQJK3xzy1TSFY1PNMXjMu6eX8IIvyWHYk1ZbJf2XWkE3QekPQsdP7cCvFi"
API_SECRET="dYqyz6G7NFFvfjIWyfqVPSSXh3dq4p8x4ryOp7tH33TgMGTwqg3K6780iSzbkGlk"
BASE_URL="https://testnet.binancefuture.com"
TIMESTAMP=$(date +%s%3N)
QUERY="timestamp=${TIMESTAMP}"
SIGNATURE=$(echo -n "$QUERY" | openssl dgst -sha256 -hmac "$API_SECRET" | awk '{print $2}')
curl -s "$BASE_URL/fapi/v2/account" -H "X-MBX-APIKEY: $API_KEY" -G -d "${QUERY}&signature=${SIGNATURE}"
