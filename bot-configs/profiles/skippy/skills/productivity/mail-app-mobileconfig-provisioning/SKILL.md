---
name: mail-app-mobileconfig-provisioning
description: Provision Apple Mail.app accounts on macOS at scale using .mobileconfig configuration profiles. Use when Tyler wants to add multiple IMAP/SMTP accounts to Mail.app without clicking through Settings for each one. Pairs with Migadu alias-on-real-mailbox pattern.
---

# Mail.app Mobileconfig Provisioning

## When to use
- Tyler wants 3+ email accounts added to Mail.app
- Migrating accounts from Envelope or another client to Mail.app
- Setting up agent send-as identities (skippy@, envelopie@, etc.) on a real mailbox

## The mechanism
`.mobileconfig` = signed/unsigned XML payload Apple uses for MDM. `PayloadType com.apple.mail.managed` adds Mail accounts. Install:

```bash
profiles install -path account.mobileconfig -user $USER
# or double-click in Finder → System Settings → Privacy & Security → Profiles → Approve
```

## Hard constraints
- **Passwords cannot ship in user-installed profiles.** Only MDM-delivered profiles can. User types each password once on first connect.
- **Unsigned profiles show scary warnings.** Signing requires Apple Developer cert ($99/yr).
- **OAuth accounts (Gmail/M365) don't work via mobileconfig.** IMAP/SMTP basic auth only. Add OAuth accounts manually in Mail.app.
- **Removing the profile removes the accounts.** Mail stays in `~/Library/Mail/` but accounts unlink.

## Account preferred model (Tyler convention)
Agent addresses are **identities/aliases on a real mailbox**, not separate mailboxes:
- `tyler@<domain>` = real IMAP mailbox
- `skippy@<domain>`, `envelopie@<domain>`, etc. = send-as identities on tyler@
- Inbound mail to alias delivers to tyler@'s inbox
- Send-as configured in Migadu admin + Mail.app account settings → "Email Aliases"

## Workflow

### 1. Audit Migadu state
```bash
# Use migadu skill — list mailboxes per domain
# Identify: which domains exist, which mailboxes are real vs aliases
```

### 2. Create alias structure (Migadu side)
For each agent address that should be an alias:
- Create real mailbox `tyler@<domain>` if missing (invitation email for password)
- Add identity `<agent>@<domain>` with send-as permission on tyler@'s mailbox
- If agent address currently exists as standalone mailbox: archive via IMAP to `.mbox` first, then delete and recreate as identity

### 3. Generate mobileconfig
Template structure (one PayloadContent per account):
```xml
<dict>
  <key>PayloadType</key><string>com.apple.mail.managed</string>
  <key>EmailAccountType</key><string>EmailTypeIMAP</string>
  <key>EmailAccountName</key><string>Tyler Martin</string>
  <key>EmailAddress</key><string>tyler@u1f99e.com</string>
  <key>IncomingMailServerHostName</key><string>imap.migadu.com</string>
  <key>IncomingMailServerPortNumber</key><integer>993</integer>
  <key>IncomingMailServerUseSSL</key><true/>
  <key>IncomingMailServerUsername</key><string>tyler@u1f99e.com</string>
  <key>OutgoingMailServerHostName</key><string>smtp.migadu.com</string>
  <key>OutgoingMailServerPortNumber</key><integer>465</integer>
  <key>OutgoingMailServerUseSSL</key><true/>
  <key>OutgoingMailServerUsername</key><string>tyler@u1f99e.com</string>
  <key>SMTPEnableTransportLayerSecurity</key><true/>
  <key>PayloadIdentifier</key><string>com.tyler.mail.tyler-u1f99e</string>
  <key>PayloadUUID</key><string>$(uuidgen)</string>
  <key>PayloadVersion</key><integer>1</integer>
</dict>
```
Wrap in standard `PayloadContent` array under outer `<plist>`.

### 4. Install
```bash
open mail-accounts.mobileconfig
# Tyler: System Settings → Privacy & Security → Profiles → Install
```

### 5. Add aliases in Mail.app (manual, per account)
- Mail → Settings → Accounts → select account → Email Address field → click dropdown → "Edit Email Addresses..."
- Add each alias (skippy@, envelopie@, etc.)
- These show up in From: dropdown when composing

## Server reference (Migadu)
- IMAP: `imap.migadu.com:993` SSL
- SMTP: `smtp.migadu.com:465` SSL/TLS
- Username = full email address
- Auth: PLAIN over SSL

## Server reference (AWS Workmail — SpainExpat)
- IMAP: `imap.mail.us-west-2.awsapps.com:993` SSL
- SMTP: `smtp.mail.us-west-2.awsapps.com:465` SSL

## Pitfalls
- Don't ship `MailNumberOfPastDaysToSync` = 0 (means "all" but slow on huge mailboxes)
- `SMTPAuthentication` defaults wrong on some macOS versions; explicitly set `SMTPEnableTransportLayerSecurity = true`
- If user has existing account with same email, profile install will conflict — remove old account first
- u1f4e7.com / u1f99e.com etc. are real domains; verify in Migadu before assuming structure

## Related skills
- `migadu` — Migadu API for mailbox/alias management
- `envelope-credential-recovery` — when Envelope breaks (Mail.app independent of Envelope)
- `email/envelope` — bot-side email CLI
