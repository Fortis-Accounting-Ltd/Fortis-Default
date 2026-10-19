# ForWork: setup guide

This guide puts ForWork online at an address like `https://forwork-fortis.azurewebsites.net` (later
`https://forwork.fortisap.com.au` if you like). Everyone signs in with their Fortis Microsoft 365 account, and
everything is backed up to OneDrive. It takes about an hour, and you don't need to write any code.

**You will need:**

- An **Azure** subscription in the firm's Microsoft 365 tenant (portal.azure.com, signed in as a Microsoft 365 admin).
  The server costs about A$20-25 a month (App Service plan *Basic B1*).
- A **Microsoft 365 / Entra admin** for step 2 and step 5 (about 15 minutes).
- Access to this GitHub repository (to deploy the code, step 4).
- A **monday.com API token** for step 6 (any admin's: monday.com > your picture > *Developers* > *My access tokens*).

Why Azure App Service: it's Microsoft's own hosting, so it sits next to your Microsoft 365. It looks after the
HTTPS certificate and the server updates, and keeps ForWork's data on a disk that survives restarts.

---

## Step 1: Create the web app (the server)

1. Go to **portal.azure.com** > **Create a resource** > **Web App**.
2. Fill in:
   | Field | What to enter |
   |---|---|
   | Resource group | *Create new*: `forwork` |
   | Name | `forwork-fortis` (this becomes the web address) |
   | Publish | **Code** |
   | Runtime stack | **Python 3.12** |
   | Operating system | **Linux** |
   | Region | **Australia East** |
   | Pricing plan | *Create new*, then **Basic B1** |
3. Click **Review + create**, then **Create**. Wait for "Your deployment is complete", then click **Go to resource**.
4. On the web app's **Overview** page, copy the **Default domain**, e.g. `forwork-fortis.azurewebsites.net`.
5. Go to **Settings > Configuration > General settings**:
   - **Startup Command**: `python -m forwork serve --host 0.0.0.0 --port 8000`
   - **Always on**: **On** (so the nightly backup runs and the first page of the day opens quickly)
   - **SCM Basic Auth Publishing Credentials**: **On** (needed for step 4)
   - Click **Save**.
6. Go to **Settings > Scale out** and check it says **1 instance**. Keep it at 1, because ForWork keeps its data
   on this one server.

## Step 2: Register ForWork for Microsoft sign-in (Entra admin)

1. Go to **entra.microsoft.com** > **Applications** > **App registrations** > **New registration**.
   - Name: `ForWork`
   - Supported account types: **Accounts in this organizational directory only**
   - Redirect URI: platform **Web**, address `https://forwork-fortis.azurewebsites.net/auth/callback`
     (your domain from step 1 plus `/auth/callback`)
   - Click **Register**.
2. On the app's **Overview** page, copy the **Application (client) ID** and the **Directory (tenant) ID**.
3. Go to **Certificates & secrets** > **New client secret**. Description `ForWork`, expires in **24 months**, then **Add**.
   Copy the **Value** now: Microsoft shows it only once. Keep it in the firm's password manager and never email it.
   Put a reminder in the calendar about a month before it expires, to make a new one (step 3 says where it goes).
4. Optional, to choose exactly who can use ForWork: go to **Enterprise applications** > **ForWork** > **Properties**,
   set **Assignment required** to **Yes**, and add people or a group under **Users and groups**. Without this,
   anyone in the firm's Microsoft 365 with a fortisap.com.au email can sign in. Their first sign-in adds them as
   a member.

## Step 3: Give ForWork its settings

In the Azure portal, open the web app > **Settings > Environment variables** > **App settings**. Add each of these
with **+ Add**, then click **Apply** at the bottom (the app restarts):

| Name | Value |
|---|---|
| `FORWORK_DATA` | `/home/forwork-data` |
| `FORWORK_BASE_URL` | `https://forwork-fortis.azurewebsites.net` (your address, no `/` at the end) |
| `FORWORK_MS_TENANT_ID` | the Directory (tenant) ID from step 2 |
| `FORWORK_MS_CLIENT_ID` | the Application (client) ID from step 2 |
| `FORWORK_MS_CLIENT_SECRET` | the secret Value from step 2 |
| `FORWORK_ADMINS` | the partners' emails, e.g. `name@fortisap.com.au,name2@fortisap.com.au` (they become admins when they sign in) |
| `FORWORK_TRUST_PROXY` | `1` |
| `FORWORK_JOURNAL_MODE` | `DELETE` (the right setting for Azure's `/home` disk) |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` |

`/home` is the part of the server's disk that Azure keeps through restarts and updates. ForWork keeps its
database, attached files and local backup copies there.

## Step 4: Put the code on the server

1. In the Azure portal, on the web app's **Overview** page, click **Download publish profile**. A small file downloads.
2. In GitHub, open this repository > **Settings** > **Secrets and variables** > **Actions** > **New repository secret**:
   - Name: `AZURE_WEBAPP_PUBLISH_PROFILE`
   - Secret: open the downloaded file in Notepad, copy all of it, and paste it here. Then **Add secret**.
   - Delete the downloaded file afterwards.
3. Still in **Secrets and variables** > **Actions**, open the **Variables** tab > **New repository variable**:
   - Name: `FORWORK_AZURE_APP`, Value: `forwork-fortis`
4. Go to **Actions** > **ForWork deploy** > **Run workflow** > **Run workflow**. It takes 3-5 minutes.
   Run it again whenever you want the server to have the latest version.
5. Open `https://forwork-fortis.azurewebsites.net`. You should see the ForWork sign-in page. Click
   **Sign in with Microsoft** and sign in. ✅

> If the page says *Microsoft sign-in is not set up*, check step 3's names and values. If Microsoft says the
> redirect URI doesn't match, check step 2.1: the address must be exactly your domain plus `/auth/callback`.

## Step 5: Backups to OneDrive or SharePoint (Entra admin + a ForWork admin)

ForWork uploads its backups with the same app registration.

1. In **entra.microsoft.com** > **App registrations** > **ForWork** > **API permissions** > **Add a permission** >
   **Microsoft Graph** > **Application permissions** > tick **Files.ReadWrite.All** > **Add permissions**.
   Then click **Grant admin consent for Fortis Accounting Partners** and confirm.
2. Decide where the backups live. A SharePoint site that only partners can open is best, e.g. a *ForWork Backups*
   library on the firm's main site. A specific person's OneDrive also works.
3. In ForWork, open **your picture (top right) > Backups**:
   - Tick **Upload backups to OneDrive or SharePoint**.
   - For SharePoint: choose *a SharePoint site's document library*, paste the site address
     (e.g. `https://fortisap.sharepoint.com/sites/Fortis`) and the library name (e.g. `Documents`).
     For a OneDrive: choose *a person's OneDrive* and enter their email.
   - Folder: `ForWork Backups`.
   - Click **Save backup settings**, then **Back up now**. After a minute, reload the page: *Last backup* should say
     **Worked**, and the files should appear in that SharePoint folder or OneDrive. ✅

Backups then run by themselves: every hour when something has changed, and every night at 2 am. Each one is a
`.db.gz` file (the whole database, used to restore) and a `.json.gz` file (every board in a readable form).

**Restoring:** on the same page, find the backup and click **Restore**, then type RESTORE. ForWork keeps a copy of
the data as it was just before the restore, so you can go back again.

## Step 6: Bring the monday.com boards across

1. In ForWork: **your picture > Import from monday.com**.
2. Paste the monday.com API token and click **Import**. A progress bar shows each board; the whole account takes a
   few minutes. The token is used once and not saved.
3. Check the result: the boards appear in the left-hand menu, with archived ones under *Archived boards*.
   The import notes list anything that couldn't come across (monday's app views, charts and forms, and updates on
   items that were deleted in monday).

People are matched by their email address. People who have left come across as inactive, so their names stay on
the work they did.

## Step 7: Switching over from monday.com

A suggested plan:

1. **Try it (a week):** two or three people use ForWork alongside monday.com. Everyone else keeps using monday.
2. **Switch-over day (e.g. a Friday afternoon):**
   1. Ask everyone to stop changing monday.com.
   2. In ForWork, **Import from monday.com** again with **Replace boards already imported** ticked. This loads every
      board fresh with that day's data. Anything typed into ForWork during the trial on those boards is replaced.
   3. Check a few boards against monday.com (item counts per group, a few statuses and updates).
   4. Tell everyone the address and that they sign in with their Microsoft account.
3. **Afterwards:** keep the monday.com subscription for a month, read only, for reference. Before it ends, download
   any files attached to monday updates that matter: ForWork lists them, but they stay in monday.com.

## Step 8 (optional): Your own address

To use `https://forwork.fortisap.com.au`:

1. Azure portal > web app > **Settings > Custom domains** > **Add custom domain**. Follow the instructions to add the
   two DNS records at your domain provider, then choose **App Service Managed Certificate** (free).
2. Entra > App registrations > ForWork > **Authentication**: add `https://forwork.fortisap.com.au/auth/callback`
   as a redirect URI.
3. Change `FORWORK_BASE_URL` (step 3) to `https://forwork.fortisap.com.au`.

---

## Looking after it

- **People:** *your picture > People*. Change someone's role (admin, member, viewer), turn their access off when
  they leave (they're signed out at once), or add someone before their first sign-in.
- **Client secret expiry:** when the secret from step 2 is about to expire, make a new one (step 2.3) and paste it
  into `FORWORK_MS_CLIENT_SECRET` (step 3). If it lapses, the sign-in page says so.
- **Something deleted by mistake:** *your picture > Bin* restores deleted boards and items. A deleted column can be
  restored from the board's *Activity log*. A deleted update is gone, but it's in last night's backup.
- **Server logs:** Azure portal > web app > **Monitoring > Log stream**.
- **Settings check:** Azure portal > web app > **Development Tools > SSH**, then run `python -m forwork check`.

## Running it somewhere else

ForWork is an ordinary Python web app, so it also runs on an office PC or any Linux server:
`python -m pip install -r requirements.txt` and `python -m forwork serve`, with the same `FORWORK_*` settings
as environment variables or in a `forwork.conf` file in the data folder (a JSON object with the same names). Put
it behind HTTPS: Azure, a reverse proxy, or Cloudflare Tunnel. Without Graph, backups can go to a OneDrive-synced
folder instead (set *Folder* on the Backups page). `deploy/Dockerfile` builds a container for a VM or Azure
Container Apps.
