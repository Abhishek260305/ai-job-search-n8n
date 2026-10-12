# A free always-on server (Oracle Cloud Always Free)

The project runs happily on your own computer, but a computer that sleeps does not search while it sleeps. A small always-on server fixes that, and Oracle Cloud's **Always Free** tier gives you one at no cost. This is the reference setup the project was built and tested on: an Ubuntu 24.04 ARM virtual machine with 2 CPUs and about 11 GB of memory. Any Ubuntu or Debian server with Docker works the same way (ARM or Intel; every image used has both); only the first half of this page is Oracle-specific.

> Cloud consoles change their wording often. If a button is not where this page says, look for the same idea. The official documentation is at <https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm>.

## 1. Create the account

Sign up at <https://www.oracle.com/cloud/free/>. You need an email address, a phone number and a **credit or debit card** for identity verification. Stay on **Always Free** resources and you are not charged. Pick your **home region** carefully: it cannot be changed later, and some regions run out of free ARM capacity. Activation can take a few minutes to a few hours, and Oracle sometimes rejects a card at first: try another card or contact their support rather than making a second account.

## 2. Create the virtual machine

In the console: **Compute, Instances, Create instance**.

- **Image:** Canonical **Ubuntu 24.04** (the ARM / aarch64 build if you choose an Ampere shape).
- **Shape:** **Ampere, `VM.Standard.A1.Flex`**, 2 OCPU and 12 GB of memory. Always Free allows up to 4 OCPU and 24 GB in total, so this leaves room. Do **not** fall back to the tiny `VM.Standard.E2.1.Micro` shape (1 GB of memory): the project needs about 4 GB, so it would not run.
- **Networking:** the default network with a **public IPv4 address**. Leave the default firewall rules alone: only SSH (port 22) is open, and **nothing in this project needs another port open**. It listens on `127.0.0.1` only, and you reach it through an SSH tunnel or Tailscale.
- **SSH keys:** choose **Generate a key pair for me** and **download the private key**, or paste a public key you already have. Keep the private key file safe: it is the only way in. On macOS and Linux run `chmod 600 <key file>` (SSH refuses a key that other people can read). On Windows, copy the key into WSL first (`cp /mnt/c/Users/<you>/Downloads/<key> ~/.ssh/`, then `chmod 600` it there) and connect from WSL: Windows folders cannot be locked down enough for SSH.

If you see **"Out of host capacity"**, try another availability domain in the same region, ask for fewer resources (1 OCPU and 6 GB is enough to start), or try again later: free capacity comes and goes.

## 3. Log in

When the instance is **Running**, copy its public IP address and connect from your computer:

```bash
ssh -i /path/to/your-key ubuntu@<public-ip>
```

The first time, answer `yes` to the fingerprint question. The login name for Ubuntu images is `ubuntu`. On Windows use WSL (see above); PowerShell's built-in `ssh` also works, but you then cannot run the project's shell scripts on your own computer.

Keep the system up to date: `sudo apt-get update && sudo apt-get upgrade -y`.

## 4. Install Docker

Use Docker's own repository, not the `docker.io` package that ships with Ubuntu (it is older). These are Docker's official steps for Ubuntu (the current version is at <https://docs.docker.com/engine/install/ubuntu/>):

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git openssl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in (so the `docker` group applies), then check: `docker compose version` prints a version.

## 5. Install the project

Continue with **[setup.md](setup.md)** from "Get the code" (step 2), or ask your AI assistant to follow [setup-with-ai.md](setup-with-ai.md). Everything runs on the server: clone the repository there, and run the `scripts/` commands there (over SSH, or with an assistant connected to the server). If you will drive it later from your laptop with `scripts/jobsearch`, clone it into a folder named `jobsearch` (`git clone <url> jobsearch`), which is where that command looks.

To see the n8n page and the dashboard from your own computer, open an SSH tunnel: `ssh -N -L 5678:127.0.0.1:5678 -L 8080:127.0.0.1:8080 -i /path/to/your-key ubuntu@<public-ip>` (run on your computer, not on the server; the repository's `scripts/tunnel.sh` does the same if you have a copy of it on your computer). For your phone, use Tailscale ([dashboard.md](dashboard.md#opening-it-from-a-phone-without-ssh-tailscale)).

## Looking after it

- **It restarts itself.** The containers are set to `restart: unless-stopped`, so a reboot of the server brings the project back by itself.
- **Back up the database, n8n and your config** (`scripts/backup.sh`, [setup.md](setup.md#backups)) and keep a copy of `.env`, especially `N8N_ENCRYPTION_KEY`, **off the server**. A free instance is not a backup service: if it is ever reclaimed or deleted, your data goes with it, and setup.md has the steps to rebuild on a new one.
- **Idle instances can be reclaimed.** Oracle may reclaim Always Free instances whose CPU, network and memory use stay very low for a week or more. This project is mostly idle between its scheduled runs, so do not assume its own work keeps the server busy. Upgrading the account to **Pay As You Go** (you are still not charged while you stay inside the Always Free limits) is the usual way to remove that risk; and keep the backups above.
- **Never open more ports.** The default firewall (SSH only) is what you want. Anything that must be reachable from outside goes through the SSH tunnel or Tailscale instead.
- **Turn the server off for good?** `docker compose down` stops the project; deleting the instance in the console removes it and its data.
