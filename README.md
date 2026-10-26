# Kindle SFTP Device for Calibre

Connect Calibre to your jailbroken Kindle wirelessly over SSH/SFTP with the full native device experience and optional zero-config remote access.

## Prerequisites

- **Jailbroken Kindle** (Paperwhite, Oasis, Basic, Scribe, etc.)
- **SSH/SFTP server running on Kindle** (port `22` or custom, e.g. Dropbear, Jobman, or KUAL helper)
- **Calibre 9.15+** on macOS, Linux, or Windows (uses system OpenSSH)

---

## Features

- 🔌 **Native Device Experience**: Mounts as a first-class Calibre device—Send to Device, automatic conversion, and library management work exactly like a physical USB connection.
- 📶 **Wireless Transfers**: Sync your books over your local Wi-Fi without searching for cables.
- 🌐 **1-Click Remote Access (Tailcat)**: Connect securely anywhere outside your home or on public Wi-Fi using encrypted WireGuard peer-to-peer tunnels without port forwarding.
- 🖼️ **Flawless Book Covers**: Automatically generates and uploads companion cover thumbnails for lockscreen and library display (immune to Amazon's cover bug).
- 📚 **Native Indexing**: Requests asynchronous native scanner indexing (`com.lab126.scanner`), with KOReader-aware skipping and a manual full-refresh action.
- ⚡ **Fast Library Caching**: Caches book metadata locally for fast incremental reconnections.
- 🔒 **Secure & Private**: Strict SSH host-key verification against `known_hosts`; no third-party cloud accounts or open firewall ports required.
- 📖 **Stock Interface**: Keep reading with Amazon's stock reader interface—no need to keep KOReader open.

> ⏳ **Note on First Connection**: The very first connection scans and indexes your existing books to build the local metadata cache. Depending on the size of your library, this initial scan can take a few minutes. Subsequent connections connect in seconds.

---

## Installation

1. Download `KindleSFTP.zip` from the latest GitHub Release (or build locally into `build/KindleSFTP.zip` with `python3 build_plugin.py`).
2. In Calibre, open **Preferences → Plugins → Load plugin from file**, select `KindleSFTP.zip` (from `build/` or downloads), and restart Calibre.
3. Click the dropdown arrow next to the new **Link Kindle Wi-Fi** toolbar button and select **Manage Connections…**.
4. Enter your Kindle's local IP address (e.g. `192.168.1.50`), port (`22`), and SSH credentials.
5. Click **Verify / trust host key…**, then **Test connection**, and click **OK**.
6. Click **Link Kindle Wi-Fi** on the toolbar to connect!

---

## Native library refresh and KOReader

After uploads, the plugin requests targeted indexing, falling back to a full scan if needed. Deletions request a full scan. These requests are asynchronous: a successful request does not prove indexing has finished or that the native library has updated onscreen.

Before requesting a scan, the plugin reads `com.lab126.appmgrd`'s `activeApp` and `activeContext`. When the shell-integration launcher is active with a context ending in `/KOReader.sh`, it skips all native scanner commands. Book transfers, cover uploads, and Calibre metadata handling continue normally. Unknown foreground state does not suppress genuine scanner errors.

After leaving KOReader and returning to Amazon's native library, open the toolbar arrow menu and select **Full refresh Kindle library**, immediately above **Preferences…**. This action requires a connected Kindle and runs through Calibre's device job queue, without changing book files. If KOReader is still active, it explains that the refresh was skipped. It does not close KOReader, restart services, or automatically retry later.

### Changes in 0.9.1

- Skip native refresh during KOReader transfers and deletions instead of failing with a missing scanner source.
- Add the manual full-refresh toolbar menu action, disabled while disconnected or already queued.

### Local verification

Run `python3 tests/integration.py` with Calibre and OpenSSH installed. The suite uses a private loopback SSH/SFTP server, synthetic books, and a disposable Calibre profile/library; it never connects to a Kindle. All 13 checks passed, including KOReader transfer/deletion without scanner calls, later manual full scans, visible scanner errors outside KOReader, and the Qt menu action executing a queued scan. The menu was also rendered and visually inspected. Actual device indexing remains unverified by these local checks.

---


## Remote Access with Tailcat (Optional)

Transfer books from outside your home network without configuring VPNs, opening router ports, or creating Tailscale accounts:

1. In **Manage Connections…**, select your local Kindle profile and click **Setup Tailcat Remote Access…**.
2. Click **Deploy Tailcat to Kindle**. The wizard automatically downloads the official static Linux release directly from `tailscale/tailcat` releases on GitHub (or you can provide a local binary), verifies and deploys it into `/mnt/us/tailcat/`, and creates a `(Tailscale)` device profile.
3. *(Host prerequisite)*: Ensure the `tailcat` client is installed on your computer (e.g. `go install github.com/tailscale/tailcat/cmd/tailcat@latest` or placed in PATH).
4. **To connect remotely:**
   - **On your Kindle**: Tap **Calibre Start Tailcat** in your home library (or launch via KUAL).
   - **In Calibre**: Select the `(Tailscale)` profile from the toolbar dropdown and click **Link Kindle Wi-Fi**.
   - **When done**: Tap **Calibre Stop Tailcat** on your Kindle.

---

## Uninstallation

### Uninstalling the Calibre Plugin
1. In Calibre, open **Preferences → Plugins**.
2. Expand **Device interface**, select **Kindle SFTP Device**, and click **Remove plugin**.
3. Restart Calibre.

### Uninstalling Tailcat from Kindle
Tailcat runs strictly in user storage (`/mnt/us/`) without modifying any root system files. You can remove it anytime:

- **Via SSH**: Run from your computer's terminal:
  ```sh
  ssh root@<kindle-ip> 'killall tailcat 2>/dev/null; rm -rf /mnt/us/tailcat /mnt/us/documents/"Calibre Start Tailcat.sh" /mnt/us/documents/"Calibre Stop Tailcat.sh" /mnt/us/extensions/tailcat'
  ```
  *(You can also click the **?** button next to Deploy Tailcat in Calibre to copy this command with your Kindle's IP filled in).*

- **Via USB Cable**: Connect your Kindle to your computer and delete:
  - The `tailcat` folder
  - In `documents/`: `Calibre Start Tailcat.sh` and `Calibre Stop Tailcat.sh`
  - In `extensions/`: `tailcat` folder (if present)

---

## License

GPLv3. See [LICENSE](LICENSE) for details.
