# Launching ROFL

Five steps, about two minutes. Do them in order — genesis is immutable, so
everything before the first push is the only chance to change the chain's
identity.

## 1. Create your key

```bash
python3 wallet.py new
```

Writes `rofl-wallet.json` (gitignored — it holds a private key in plain text).
Note the address it prints.

## 2. Mine genesis

```bash
python3 make_genesis.py \
  --miner ram0verflow \
  --message "your genesis message, max 80 bytes" \
  --address rofl1...your-address...
```

Takes about thirty seconds. This writes `chain/blocks.jsonl`, renders the
README and generates `assets/ledger.svg`.

**The message is permanent.** Bitcoin's genesis carries a newspaper headline
from the day it was mined, which is what proves the chain wasn't premined.
Yours should be something you'd want quoted back at you.

Check it:

```bash
python3 verify.py
```

## 3. Create the repo

The profile README has to live in a repo named exactly your username.

```bash
gh repo create ram0verflow/ram0verflow --public \
  --description "A small Bitcoin-like chain whose ledger is my README"
git init && git add . && git commit -m "ROFL: genesis"
git branch -M main
git remote add origin https://github.com/ram0verflow/ram0verflow.git
git push -u origin main
```

## 4. Open the first two submission issues

Both need the label `rofl` — the workflow ignores comments anywhere else.

```bash
gh label create rofl --description "ROFL chain submissions" --color 8A5F10
gh label create rofl-active --description "Current writable ROFL submission issue" --color 2DA44E

block_issue=$(gh issue create --title "Mine a block" --label rofl --body \
"Paste your \`rofl-block-v1:\` line as a comment. See the README to mine one.")

gh issue create --title "Mempool" --label rofl --body \
"Paste your \`rofl-tx-v1:\` line as a comment to queue a transaction."

gh issue edit "$block_issue" --add-label rofl-active
```

The active label is the stable target used by the README links. A maintenance
workflow moves it to a fresh issue once the current one reaches 2200 comments,
before GitHub disables comments at 2500. The second issue is the first standby;
after that, the workflow creates new submission issues automatically.

Pin both from the issue page so visitors find them.

Pull-request transactions need no setup — `rofl-pr.yml` is already wired to
accept a single added file under `chain/pending/`.

## 5. Check Actions can write

Settings → Actions → General → Workflow permissions →
**Read and write permissions**. Without this the node can validate but not
commit, and every submission will be rejected at the push step.

## 6. Turn on the explorer

Settings → Pages → Source: **Deploy from a branch**, branch `main`, folder
**`/docs`**. A minute later the explorer is live at
`https://ram0verflow.github.io/ram0verflow/`.

Until the chain exists it falls back to a bundled demonstration chain, clearly
labelled, so the page is never blank.

If you publish under a different repo name, edit one attribute in
`docs/index.html`:

```html
<html lang="en" data-repo="YOUR_USER/YOUR_REPO">
```

## Then mine block 1

```bash
python3 miner.py --miner ram0verflow --message "block one"
```

Paste the output on issue #1 and watch the README update itself.

---

## Tuning

Everything is at the top of `rofl/consensus.py`:

| Constant | Effect |
|---|---|
| `GENESIS_BITS` | starting difficulty — `0x1e010000` is 15 puzzles, ~30s |
| `TARGET_SPACING` | what the retarget aims for, in seconds |
| `RETARGET_INTERVAL` | how often difficulty adjusts |
| `HALVING_INTERVAL` | blocks between subsidy halvings |
| `COINBASE_MATURITY` | confirmations before a reward can be spent |
| `MAX_TXS_PER_BLOCK` | raise it if validation time stays comfortable |
| `MAX_MEMO_BYTES` | how much text can ride along with a transfer |

And in `rofl/pow.py`:

| Constant | Effect |
|---|---|
| `N` | puzzle size. 40 needs ~200 MB to solve; 44 needs ~280 MB |
| `K_MAX` | ceiling on puzzles per block, so a block stays verifiable |

Change any of these **before** genesis. Changing them afterwards invalidates
the existing chain, and `verify.py` will tell you so.
