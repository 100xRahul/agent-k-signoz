# agentk-bench — Results

_Run: 2026-07-25T16:44:56.980219+00:00 · 1 run(s)/class · agent http://localhost:9000_

## Headline

| Metric | Score |
|---|---|
| Detection | 100% (4/4) |
| Localization (right service) | 100% (4/4) |
| Classification (right fault signature) | 100% (4/4) |
| Remediation (right guarded action) | 67% (2/3) |
| Groundedness (independent auditor) | 100% (4/4) |
| **False-alarm rate (healthy control)** | **0% (0/1)** |

Verdicts are computed deterministically from each investigation's stored root cause, report, proposed actions, and auditor verdict — a run cannot pass on a hallucinated narrative.

## Per-run detail

| Class | Investigation | Status | Detect | Local | Class | Remed | Grounded | False-alarm |
|---|---|---|:-:|:-:|:-:|:-:|:-:|:-:|
| healthy-control | `00c868ca3560` | done | — | — | — | — | ✅ | ❌ |
| bad-deploy | `03a9bd3486f1` | done | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| pool-exhaustion | `1e44e75e4845` | done | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| flag-combo | `3c1dbbdb0dfe` | done | ✅ | ✅ | ✅ | ❌ | ✅ | — |
| secret-leak | `ebedd189e6f8` | done | ✅ | ✅ | ✅ | — | ✅ | — |

