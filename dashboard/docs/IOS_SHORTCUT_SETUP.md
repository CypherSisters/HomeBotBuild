# Send to Puff from your iPhone (Apple Shortcut)

iOS can't add a website to the Share Sheet, so "Share → Puff" is a small Apple
Shortcut you build once. It POSTs the shared photo or file to Puff using a
**device token** — an upload-only key you generate on the dashboard.

The token can do exactly one thing: add a document to Puff's knowledge. It
**cannot** control the house, open cameras, or read anything back. If a phone is
lost, revoke its token on the dashboard and it stops working immediately.

## 1. Generate a device token

1. On a paired device, open **Send to Puff** (`/send`) or the dashboard's
   knowledge page.
2. Under **Set up phone sharing**, tap **Create a share shortcut**.
3. Copy the token (`pstk_…`). **It is shown only once** — if you lose it,
   revoke it and make a new one.

## 2. Build the Shortcut

In the **Shortcuts** app on the iPhone:

1. **+** (new shortcut) → **Add Action** → search **Get Contents of URL**.
2. Tap the shortcut settings (the ⓘ / name area) → **Show in Share Sheet** →
   set **Accept** to **Images** and **Files**.
3. Configure **Get Contents of URL**:
   - **URL:** `http://<your-box-address>:3003/api/pai/knowledge/upload`
   - **Method:** `POST`
   - **Headers:** add one —
     - Key: `Authorization`
     - Value: `Bearer pstk_…`  *(the token from step 1, with `Bearer ` in front)*
   - **Request Body:** `Form`
     - Field `file` → type **File**, value = **Shortcut Input**
     - Field `zone` → type **Text**, value = `2`
       *(2 = general knowledge; use `1` for the PIN-gated personal vault)*
4. Name it **Send to Puff** and confirm **Show in Share Sheet** is on.

## 3. Use it

In Photos or Files, tap **Share → Send to Puff**. The photo/file uploads to Puff
and gets indexed, exactly like dropping it on the `/send` page.

## Notes & limits

- **Same Wi-Fi only.** The URL is Puff's LAN address; the Shortcut works when the
  phone is on the house network. Off-network it will fail to connect.
- **Plain HTTP.** Like the rest of the LAN dashboard, the upload is unencrypted
  on the local network. The token is upload-only, so the worst case for a
  captured token is an unwanted document upload — never house control.
- **Expiry.** Tokens last 180 days. Make a fresh one when it expires.
- **Revoke anytime.** The dashboard's phone-sharing panel lists every token with
  a **Revoke** button. Revoking is instant.
- **Wrong address?** If the appliance's IP changes, update the URL in the
  Shortcut to match the new `http://<your-box-address>:3003/api/pai/knowledge/upload`.
