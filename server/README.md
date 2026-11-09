# LCClips share server

This is a single Cloudflare Worker backed by an R2 bucket. LCClips uploads a clip to it and gets back a link like `https://lcclips.<you>.workers.dev/c/k3J9xQmP`. The link opens a player page, and Discord, X and iMessage show the video playing inline.

**Most people don't need this page.** In LCClips, choose **Help → Set up share links…** and the wizard does all of this for you, with no Node.js or scripts. The steps below are the manual, advanced alternative.

## Manual setup (advanced)

You need **Node.js LTS** (from nodejs.org) and a free **Cloudflare account**.

1. In the Cloudflare dashboard, go to **R2** and click **Enable R2**.
   Cloudflare asks for a card. You aren't charged under the free tier: 10 GB of storage, and downloads (bandwidth) are always free.
2. In LCClips, choose **Help → Set up share links…** to open the `server` folder, then double-click **`setup.bat`** in it.
   (If you're running from source, it's the repo's `server\` folder.)

The script does everything else:
- installs wrangler into this folder (nothing global);
- opens your browser once so you can log in to Cloudflare;
- creates the `lcclips` bucket;
- deploys the Worker;
- generates a random upload key and stores it on the Worker as a secret;
- **writes the server URL and upload key into LCClips' settings.**

Restart LCClips afterwards and click **Test** under *Share links* to confirm the connection.

If this is your first Worker, Cloudflare may ask you to choose a `workers.dev` subdomain. Pick any name and run `setup.bat` again.

## Updating or redeploying

Run `setup.bat` again. It's safe to repeat and keeps your existing upload key.

If your key leaks, run `setup.bat -NewToken`. That creates a new key; the old one stops working right away, and existing links keep working.

## Using your own domain (optional)

In the Cloudflare dashboard, go to **Workers & Pages → lcclips → Settings → Domains & Routes → Add → Custom domain**, for example `clips.yourname.com`. Then put that URL in LCClips under *Share links → Server URL*.

## Auto-deleting old clips

LCClips handles this itself, so nothing needs setting up on Cloudflare. Shared clips are deleted from your server **30 days** after you share them.
You can change the number of days in **Settings → Share links**; 0 keeps them forever. The clips saved on your PC are never touched.
LCClips runs the cleanup when it starts and then every hour while it's running in the tray.

## How it works

| Route | What it does |
|---|---|
| `GET /c/:id` | Share page: video player, title, date, Download button, and the embed tags for Discord/X |
| `GET /v/:id.mp4` | The video, with seeking (Range) support |
| `GET /t/:id.jpg` | Thumbnail used in embeds |
| `POST /api/upload` … `/complete` | Upload in 50 MB parts, which gets around the Workers 100 MB request limit (needs the upload key) |
| `DELETE /api/clips/:id` | Delete a clip (needs the upload key). Available in LCClips under **My uploads… → Delete from server** |

Clip IDs are 8 random characters, so links can't be guessed. Anyone you send a link to can watch, but nobody can browse your clips.

## Local testing

```bash
cd server
npm install
echo UPLOAD_TOKEN=devtoken > .dev.vars
npx wrangler dev
```

That runs the Worker at http://127.0.0.1:8787 with a local, simulated bucket. Point LCClips at it with the upload key `devtoken`.
