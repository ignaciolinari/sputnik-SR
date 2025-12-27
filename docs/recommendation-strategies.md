# Recommendation Strategies

This document describes all recommendation strategies implemented in Sputnik-SR.

## Table of Contents

1. [RRF-Ensemble](#rrf-ensemble)
2. [Max-Ensemble](#max-ensemble)
3. [Hybrid Engine](#hybrid-engine)
4. [Advanced Recommendations (NMF + Two Towers)](#advanced-recommendations-nmf--two-towers)
5. [Matrix Factorization (NMF)](#matrix-factorization-nmf)
6. [Two Towers (Deep Learning)](#two-towers-deep-learning)
7. [Co-occurrence (release_pairs)](#co-occurrence-release_pairs)
8. [Content Profiles](#content-profiles)
9. [Popularity](#popularity)
10. [Random Exploration](#random-exploration)
11. [Contextual Recommendations](#contextual-recommendations)
12. [Evaluation Metrics](#evaluation-metrics)
13. [Global Configuration](#global-configuration)
14. [Strategy Summary](#strategy-summary)
15. [Future Strategies](#future-strategies)
16. [References](#references)

---

## RRF-Ensemble

**Function:** `recommend_rrf_ensemble(user_id, limit=9, k=None)`
**Status:** Stable (k=10 by default, calibrated offline)
**API Endpoint:** `/api/recommend/<user_id>/rrf_ensemble`

System that combines multiple strategies using **Reciprocal Rank Fusion (RRF)**, a proven Information Retrieval technique for merging ranked lists from multiple sources. Unlike max-ensemble (which takes the maximum score), RRF rewards **agreement** between algorithms.

### Mechanics

1. **Generate candidates** from each available strategy using the same cap (`limit * Config.rrf_candidate_multiplier`, default 4×):
    - `pairs`
    - `content`
    - `nmf` (available from 20+ ratings)
    - `two_towers` (available from 30+ ratings)
    - **Popular** is used only as fallback when there are no signals (cold start), to avoid contaminating the final ranking.

2. **Compute RRF score** for each candidate release:

    ```python
    RRF_score(item) = Σ 1/(k + rank_i)
    ```

    Where:
    - `rank_i` = the item position in ranking i (1-indexed)
    - `k` = smoothing constant (default: 10 in Sputnik; optimized for this use case but adjustable)

3. **Sort all releases** by descending RRF score

4. **Diversify by artist** and return top-K

### Automatic Adaptation

The system adapts based on user interactions:

| Interactions | Active Strategies | Behavior |
|---------------|-------------------|----------|
| **0** | Popular (fallback) | Cold start |
| **1-19** | pairs + content | Combines co-occurrence and content |
| **20-29** | pairs + content + nmf | Adds matrix factorization |
| **30+** | pairs + content + nmf + two_towers | All strategies available |

### Concrete Example

User with 25 positive ratings:

```
pairs generates:    Release A (pos 1), Release B (pos 3), Release C (pos 5)
content generates:  Release B (pos 2), Release C (pos 1), Release D (pos 4)
nmf generates:      Release A (pos 2), Release D (pos 1), Release E (pos 3)

RRF scores (k=10):
  A → 1/(10+1) + 1/(10+2) = 0.0909 + 0.0833 = 0.1742
  B → 1/(10+3) + 1/(10+2) = 0.0769 + 0.0833 = 0.1602
  C → 1/(10+5) + 1/(10+1) = 0.0667 + 0.0909 = 0.1576
  D → 1/(10+4) + 1/(10+1) = 0.0714 + 0.0909 = 0.1623
  E → 1/(10+3) = 0.0769

Final ranking: [A, B, D, C, E, ...]
```

**Observation:** Release A appears high in both pairs and nmf, so it accumulates more RRF score.

### Advantages vs Max-Ensemble

1. **Rewards agreement:** if multiple algorithms recommend a release, it accumulates more score
2. **No normalization needed:** RRF works directly on ranks, not comparable scores
3. **Proven technique:** RRF is standard in Information Retrieval (used in search systems)
4. **Controlled k parameter:** default=10 (recalibrate via `offline_recommender/search_rrf_k.py` if needed).

### Comparison with Max-Ensemble

| Aspect | Max-Ensemble | RRF-Ensemble |
|---------|--------------|--------------|
| **Philosophy** | “Best score wins” | “Agreement across algorithms” |
| **Works best when** | One algorithm is highly confident | Multiple algorithms agree |
| **Average NDCG** | ~0.5835 | ~0.5780 (virtually tied) |
| **Main advantage** | Preserves strong individual scores | Detects consensus across sources |
| **Complexity** | Simple (MAX) | Medium (sum of reciprocals) |

**Evaluation results (200 users):**
- Max wins: 40.0% of users
- RRF wins: 32.5% of users
- Ties: 27.5% of users
- Average difference: -0.0055 (virtually tied)

### k Parameter

The `k` parameter controls rank “smoothing”:

- **Low k (5-12)**: strongly prioritizes top positions (current default: 10)
- **Medium k (15-40)**: balances agreement and individual position
- **High k (60+)**: most weight goes to “how often it appears” rather than exact order

**Tip:** Run `python offline_recommender/search_rrf_k.py --k-values 5 8 10 12 15 20 30` to evaluate multiple values on the same users/holdouts and pick what maximizes NDCG or Hit-Rate.

**Recent data:** in the November 2025 offline evaluation (200 users with ≥40 ratings), `k=10` achieved NDCG@9 = 0.6063, outperforming the other tested values (5, 8, 9, 11, 12, 13, 15, 20, 30, 45, 60, 100). That’s why it is the current default.

### Why it works

**Complementarity + agreement:** different strategies capture different aspects:
- `pairs`: direct item-to-item connections
- `content`: genre/artist preference signals
- `nmf`: latent preference patterns
- `two_towers`: deep learning over richer features

When multiple strategies agree on a release, it is more likely to be relevant.

### Usage

```python
# Via Python
from app import recommender
recommendations = recommender.recommend_rrf_ensemble("user_id", limit=9)

# Via API
GET /api/recommend/<user_id>/rrf_ensemble?limit=9&k=10&format=full
```

### Configuration

- **k**: `Config.rrf_default_k` (default: 10, calibratable with `search_rrf_k.py`)
- **Candidate multipliers**: `Config.rrf_candidate_multiplier` (default: 4× `limit` for all strategies)
- **Per-strategy weights**: `Config.rrf_strategy_weights` (lets you increase/decrease each signal’s contribution if future metrics justify it)

---

## Max-Ensemble

**Function:** `recommend_max_ensemble(user_id, limit=9)`
**Status:** Stable
**API Endpoint:** `/api/recommend/<user_id>/max_ensemble`

System that combines multiple strategies by selecting the **maximum score** for each candidate release. Unlike the previous hybrid approach (which chose ONE strategy), max-ensemble runs ALL available strategies and keeps the best signal for each item.

### Mechanics

1. **Generate candidates** with each available strategy:
    - `pairs`: 3× candidates (high weight, best for 64.7% of users)
    - `content`: 1.5× candidates (medium weight, best for 26.3% of users)
    - `advanced`: 1× candidates (base weight, best for 6.5% of users)

2. **For each candidate release**, keep the HIGHEST score across all strategies

3. **Sort all releases** by their maximum score

4. **Diversify by artist** and return top-K

### Automatic Adaptation

The system adapts based on user interactions:

| Interactions | Active Strategies | Behavior |
|---------------|-------------------|----------|
| **0** | Popular | Cold start (same as hybrid) |
| **1-19** | pairs + content | Combines co-occurrence and content |
| **20+** | pairs + content + advanced | All available strategies |

### Concrete Example

User with 25 positive ratings:

```
pairs generates:    Release A: 0.95, Release B: 0.80
content generates:  Release B: 0.70, Release C: 0.85
advanced generates: Release A: 0.50, Release D: 0.75

Max-Ensemble combines:
  A → MAX(0.95, 0.50) = 0.95 ✓ (preserves the best)
  C → 0.85 ✓
  B → MAX(0.80, 0.70) = 0.80 ✓
  D → 0.75 ✓

Final ranking: [A, C, B, D, ...]
```

### Advantages vs the previous Hybrid

1. **Does not pick a single strategy:** uses strengths of all strategies simultaneously
2. **No averaging that dilutes signals:** preserves best scores
3. **Implicit diversity:** different releases may come from different strategies
4. **No hyperparameters:** does not require calibrating weights or thresholds

### Why it works better

**Real complementarity:** different strategies perform better for different users:
- `pairs` works best for 64.7% of users
- `content` works best for 26.3% of users
- `advanced` works best for 6.5% of users

The maximum captures the “winner” per release, whereas the old hybrid picked one strategy for the whole user.

### Usage

```python
# Via Python
from app import recommender
recommendations = recommender.recommend_max_ensemble("user_id", limit=9)

# Via API
GET /api/recommend/<user_id>/max_ensemble?limit=9&format=full
```

---

## Hybrid Engine

**Function:** `recommend(user_id, limit=9)`

The hybrid engine combines all other strategies based on the user’s history. It automatically selects the best available strategy and uses fallbacks when needed.

### Decision Logic

1. **No positive ratings:**
    - Uses popularity recommendations
    - If candidates are still missing, fills with random items

2. **With ≤8 positive ratings:**
    - Prioritizes co-occurrence (`recommend_from_pairs`)
    - If candidates are missing, fills with popularity
    - If still missing, adds random

3. **With 9 positive ratings:**
    - Prioritizes content profiles (`recommend_content_based`)
    - If candidates are missing, fills with popularity
    - If still missing, adds random

4. **With ≥20 positive ratings:**
    - Prioritizes advanced recommendations (`recommend_advanced`) if embeddings are available
      - **Level 1 (20-29 ratings)**: NMF only
      - **Level 2 (≥30 ratings)**: combines NMF + Two Towers with dynamic weights and an agreement bonus
    - If advanced recommendations are not available, uses content profiles as fallback
    - If candidates are missing, fills with popularity
    - If still missing, adds random

5. **Final diversification:**
    - Applies artist-level diversification (`_diversify_by_artist`)
    - Prioritizes different artists
    - Avoids repeating the same artist in the recommendations

### Flow Example

```
User with 5 positive ratings:
1. Tries co-occurrence → finds 6 candidates
2. Fills with popularity → finds 3 more
3. Diversifies by artist → reorders to avoid repeats
4. Returns top 9 recommendations
```

---

## Advanced Recommendations (NMF + Two Towers)

**Function:** `recommend_advanced(user_id, limit=9)`

Unified system that combines NMF and Two Towers depending on the user level. It automatically activates for users with ≥20 positive ratings when embeddings are available.

### Unlock Levels

The system has two progressive levels:

- **Level 1 (≥20 positive ratings)**: enables NMF only
- **Level 2 (≥30 positive ratings)**: enables NMF + Two Towers combination

### Combination Logic (Level 2)

When both systems are available at level 2:

1. **Fetch candidates from both systems:**
    - NMF: top `limit * 3` candidates
    - Two Towers: top `limit * 3` candidates

2. **Normalize scores by position:**
    - Each candidate receives a normalized [0, 1] score based on its position
    - Best position = higher score (1.0 for the first)

3. **Combine scores with dynamic weights:**

    ```python
    # Weights adapt to user history:
    # - 30-50 ratings: 50% NMF, 50% Two Towers
    # - 51-100 ratings: 40% NMF, 60% Two Towers
    # - 101-200 ratings: 30% NMF, 70% Two Towers
    # - 201+ ratings: 20% NMF, 80% Two Towers
    combined_score = nmf_weight * nmf_score + two_towers_weight * two_towers_score
    ```

4. **Apply an agreement bonus:**
    - If a candidate appears in both systems: add `+0.2` to the combined score
    - This prioritizes recommendations with agreement between models

5. **Sort and return top-k:**
    - Sort by descending combined score
    - Return top `limit`

### Smart Fallbacks

- If only NMF is available: use NMF only
- If only Two Towers is available: use Two Towers only
- If neither is available: return an empty list (hybrid engine falls back to content-based)

### On-demand Updates

Users can update their embeddings from the web UI using the unified **“Advanced recommendations”** button:

- **Level 0 (<20 ratings)**: disabled button, shows progress like “15/20”
- **Level 1 (20-29 ratings)**: enabled, updates NMF only
- **Level 2 (≥30 ratings)**: enabled, updates NMF + Two Towers

**Endpoint:** `POST /actualizar-recomendaciones-avanzadas`

The endpoint automatically detects the user level and updates the relevant systems.

### Advantages of the unified system

- **Simplified UX**: one button instead of two technical actions
- **Clear progression**: user sees progress toward the next level
- **Better quality**: level-2 combination leverages both systems
- **Agreement**: the agreement bonus prioritizes more reliable results
- **Robust fallbacks**: still works if one system fails

### Configuration

- **`min_advanced_level_1_signals`**: `20` - threshold for level 1 (NMF)
- **`min_advanced_level_2_signals`**: `30` - threshold for level 2 (NMF + Two Towers)
- **Dynamic weights**: adjusted based on history (see previous section)
- **`advanced_consensus_bonus`**: `0.2` - bonus for candidates present in both systems

---

## Matrix Factorization (NMF)

**Function:** `recommend_nmf(user_id, limit=9)`

System based on **Non-negative Matrix Factorization (NMF)** that learns latent patterns from user preferences. It is activated automatically as part of **Advanced Recommendations** for users with ≥20 positive ratings when embeddings are available. Level 1 uses NMF only; level 2 combines it with Two Towers.

### Embedding Build (Offline)

**Script:** `offline_recommender/build_nmf_embeddings.py`

#### Process:

1. **Data filtering:**
    - Filters interactions with `rating >= 3.0` (configurable)
    - Includes only users with ≥10 positive ratings (configurable)
    - Includes only releases with ≥5 positive ratings (configurable)

2. **Sparse matrix construction:**
    - Builds a user–item matrix in CSR (Compressed Sparse Row) format
    - Stores only non-zero values (memory efficient)
    - Typically uses ~50-100 MB for large datasets

3. **NMF training:**
    - Factorizes the matrix as: `matrix ≈ user_embeddings @ item_embeddings.T`
    - Learns latent factors (default: 50 components)
    - Uses L1/L2 regularization to avoid overfitting
    - Typically converges in 50-100 iterations

4. **Storage:**
    - Stores user embeddings in table `user_embeddings`
    - Stores release embeddings in table `release_embeddings`
    - Each embedding is a latent-factor vector (JSON array)

### Real-time Recommendation

#### `recommend_nmf()` algorithm:

1. **Load user embedding:**
    - Looks up the embedding in `user_embeddings`
    - If not found, returns an empty list (hybrid falls back to content-based)

2. **Compute cosine similarity:**
    - For each release with an embedding:

      ```python
      similarity = dot(user_embedding, release_embedding) / (
                        norm(user_embedding) * norm(release_embedding)
                        )
      ```

3. **Filter and rank:**
    - Excludes releases already seen/rated by the user
    - Sorts by descending similarity
    - Returns top-k

### Current Configuration

- **Activation threshold:** `min_nmf_signals = 20` (users with ≥20 positive ratings)
- **Latent components:** `n_components = 50` (configurable during build)
- **Data filters:** `min_user_ratings = 10`, `min_release_ratings = 5` (configurable)
- **Max iterations:** `max_iter = 200` (typically converges earlier)

### Practical Example

**User with 25 positive ratings:**

1. Hybrid system detects ≥20 ratings
2. Tries `recommend_nmf()`
3. Loads user embedding (50-dimensional vector)
4. Computes similarity vs ~109k releases with embeddings
5. Returns the top 9 most similar recommendations

**If embeddings are not available:**
- Automatic fallback to `recommend_content_based()`
- The system continues working normally

### Advantages

- **Captures complex patterns**: latent factors find non-obvious relationships
- **Very memory efficient**: sparse matrices use ~50-100 MB vs ~17 GB dense
- **Scalable**: fast inference (<100ms) even with many releases
- **Better for active users**: improves with more user data
- **Diversity**: can surface releases beyond obvious genres/artists

### Limitations

- **Requires precomputed embeddings**: must be generated offline periodically
- **Cold start**: does not work well for new users (<20 ratings)
- **Data-quality dependent**: needs enough positive interactions
- **Less interpretable**: latent factors do not have a direct meaning

### Embedding Updates

Embeddings can be updated in two ways:

#### 1. On-demand update (Recommended)

Users with ≥20 positive ratings can generate/update their individual embedding from the web UI using the unified **“Advanced recommendations”** button next to their username. The system detects the level and updates NMF (level 1) or NMF + Two Towers (level 2).

**Advantages:**
- Immediate update when the user rates new releases
- Recomputes only the user embedding (very fast, <1 second)
- No server access or offline scripts required
- Available directly from the UI

**How it works:**
- Computes a weighted average of embeddings for releases the user rated positively
- Uses precomputed release embeddings (which must exist)
- Stores the new user embedding in the database

#### 2. Periodic offline update (for releases)

Release embeddings must be rebuilt periodically when there are new interactions:

```bash
# Rebuild release embeddings (typically weekly)
python -m offline_recommender.build_nmf_embeddings \
     --n-components 50 \
     --min-user-ratings 10 \
     --min-release-ratings 5 \
     --verbose
```

**Estimated time**: 1-2 minutes for medium/large datasets

**Note**: Users can update their own embedding at any time from the UI, but release embeddings should be regenerated offline periodically to include new releases and refresh global latent patterns.

### Comparison with other strategies

| Aspect | NMF | Content-based | Release Pairs |
|---------|-----|---------------|--------------|
| **Complexity** | High | Medium | Low |
| **Cold start** | Poor | Good | Good |
| **Active users** | Excellent | Good | OK |
| **Diversity** | High | Medium | Low |
| **Interpretability** | Low | High | Medium |
| **Memory** | Medium | Low | Low |

---

## Two Towers (Deep Learning)

**Function:** `recommend_two_towers(user_id, limit=9)`

Deep-learning system that uses a Two Towers architecture to learn separate embeddings for users and items based on features and preferences. It activates automatically as part of **Advanced Recommendations** at level 2 (≥30 positive ratings), where it is combined with NMF for better recommendations.

### Architecture

The model consists of two separate neural networks:

1. **User Tower**
    - Encodes user features (role, objectivity_score, soundoffs, ratings_count, days since registration/activity)
    - Output: user embedding (dimension configurable, default: 64)

2. **Item Tower**
    - Encodes release features (artist_id, release_type, genres, year, avg_rating, ratings_count)
    - Output: release embedding (same dimension)

3. **Scoring**
    - Dot product between normalized embeddings (equivalent to cosine similarity)
    - Embeddings are L2-normalized for efficiency

### Embedding Build (Offline)

**Script:** `offline_recommender/build_two_towers.py`

#### Process:

1. **Data filtering:**
    - Filters interactions with `rating >= 3.0` (configurable)
    - Includes only users with ≥5 positive ratings (configurable)
    - Includes only releases with ≥3 positive ratings (configurable)
    - Optionally limits with `--sample-size` for quick tests

2. **Feature extraction:**
    - **Users**: role, objectivity_score, soundoffs, ratings_count, days since registration/activity
    - **Releases**: artist_id, release_type, genres (multi-hot), release_year, avg_rating, ratings_count
    - Normalization and transforms (log, scaling, etc.)

3. **Model training:**
    - Keras/TensorFlow architecture
    - Categorical embeddings (role, artist, type, genres)
    - Dense layers for numeric features
    - Dropout for regularization
    - Configurable negative sampling (`--num-negatives`, default 4) to build positive/negative pairs per user
    - Loss: Binary Cross-Entropy with logits + class weights (upweights positives)
    - Online metrics: `binary_accuracy` and `AUC` to monitor convergence
    - Optimizer: Adam with configurable learning rate
    - Callbacks: Early stopping and ReduceLROnPlateau (monitoring AUC)

4. **Storage:**
    - Stores user embeddings in table `user_embeddings_dl`
    - Stores release embeddings in table `release_embeddings_dl`
    - Each embedding includes dimension and model version

### Real-time Recommendation

#### `recommend_two_towers()` algorithm:

1. **Load user embedding:**
    - Looks up the embedding in `user_embeddings_dl`
    - If missing, returns an empty list (hybrid falls back to content-based)

2. **Compute similarity:**

    ```python
    # Embeddings are already L2-normalized, so dot product = cosine
    similarity = dot(user_embedding, release_embedding)
    ```

3. **Filter and rank:**
    - Excludes releases already seen/rated by the user
    - Sorts by descending similarity
    - Returns top-k

### Current Configuration

- **Activation threshold:** enabled at advanced level 2 (≥30 positive ratings)
- **Embedding dimension:** `embedding_dim = 64` (configurable)
- **Data filters:** `min_user_ratings = 5`, `min_release_ratings = 3` (configurable)
- **Epochs:** `epochs = 10` (configurable, with early stopping)
- **Batch size:** `batch_size = 1024` (configurable)

**Note:** Two Towers is primarily used in combination with NMF at advanced level 2, leveraging both systems’ strengths.

### Practical Example

**User with 35 positive ratings (Level 2):**

1. Hybrid detects ≥30 ratings (level 2)
2. Calls `recommend_advanced()` (NMF + Two Towers)
3. Fetches candidates from both systems
4. Combines scores with weights (40% NMF, 60% Two Towers)
5. Applies agreement bonus (+0.2) to candidates present in both
6. Returns top 9 with the best combined score

**If embeddings are not available:**
- Automatic fallback to `recommend_content_based()`
- The system continues working normally

### Advantages

- **Better feature usage**: uses user/item features that NMF does not
- **Improved cold start**: can recommend using static features without a long history
- **Non-linear patterns**: neural nets can model complex relationships
- **Flexibility**: easy to add new features without changing the architecture
- **Scalable**: fast inference with precomputed embeddings
- **Complementary**: coexists with NMF and can be used depending on the scenario

### Limitations

- **Requires precomputed embeddings**: must be generated offline periodically
- **Training time**: slower than NMF (minutes vs seconds)
- **Hyperparameters**: requires tuning (architecture, learning rate, etc.)
- **Dependencies**: requires TensorFlow/Keras
- **Memory**: heavier than NMF (though embeddings are similar)
- **Less interpretable**: embeddings have no direct meaning

### Model Training

Embeddings should be generated offline using the build script:

```bash
# Full training (recommended)
python -m offline_recommender.build_two_towers \
     --database data/sputnik.db \
     --embedding-dim 64 \
     --epochs 10 \
     --batch-size 1024 \
     --min-user-ratings 5 \
     --min-release-ratings 3 \
     --verbose

# Quick test with a subset
python -m offline_recommender.build_two_towers \
     --database data/sputnik.db \
     --sample-size 50000 \
     --epochs 5 \
     --verbose
```

**Estimated time**:
- Quick test (50k interactions): ~30 seconds
- Full training (8M interactions): 3-6 hours on CPU

**Checkpoints and resume:**
- `--checkpoint-path`: saves best combined-model weights at the end of each epoch (weights only). Recommended: `models/Two Towers/checkpoints/*.weights.h5`.
- `--resume-from-checkpoint`: loads those weights before training and continues the run (keeps optimizer state). Useful if the machine reboots or if you want to keep refining a previous model run.

This lets you split long trainings across multiple sessions without losing progress.

### Automatic NDCG@k evaluation

The same script can hold out interactions per user and compute NDCG@k without running `evaluate_recommender.py`.

- Enable it with `--evaluate-ndcg`. The `--ndcg-holdout` parameter (default 0.2) defines which fraction of positive interactions is held out per user, and `--ndcg-min-test-items` ensures each user has enough holdout items.
- `--ndcg-k` controls the evaluated ranking size (default 9), and `--ndcg-max-users` can cap the number of evaluated users for fast runs.
- The pipeline splits interactions, trains the model on the training split, then evaluates using the newly trained towers. The metric is recorded in `models/Two Towers/two_towers_<db>_metadata.json` along with number of evaluated users and holdout parameters.
- This mirrors the automated NMF flow (which optimizes with NDCG) and provides traceability for each run so models can be compared without extra scripts.

### Embedding Updates

Embeddings can be updated in two ways:

#### 1. On-demand update (Recommended)

Users with ≥20 positive ratings can generate/update embeddings from the web UI using the unified **“Advanced recommendations”** button next to their username. The system detects the level and updates the corresponding systems (NMF at level 1, NMF + Two Towers at level 2).

**Advantages:**
- Immediate update when the user rates new releases
- Tries to use the trained model if available; otherwise uses a weighted-average approximation
- No server access or offline scripts required
- Available directly from the UI

**How it works:**
- If a trained model is available, it generates the embedding from user features
- If the model is not available, it computes a weighted average of embeddings for releases the user rated positively
- Uses precomputed release embeddings (which must exist)
- Stores the new user embedding in the database

#### 2. Periodic offline update (for releases + model)

Release embeddings and the model should be rebuilt periodically when there are new interactions:

```bash
# Rebuild embeddings and model (typically weekly)
python -m offline_recommender.build_two_towers \
     --database data/sputnik.db \
     --embedding-dim 64 \
     --epochs 10 \
     --batch-size 1024 \
     --min-user-ratings 5 \
     --min-release-ratings 3 \
     --verbose
```

**Estimated time**: 3-6 hours for large datasets (CPU)

**Note**: Users can update their individual embeddings at any time from the UI. The model and release embeddings must be regenerated offline periodically to include new releases and refresh globally learned patterns.

### Comparison with NMF

| Aspect | NMF | Two Towers |
|---------|-----|------------|
| **Minimum threshold** | 20 ratings | 10 ratings |
| **Features** | Ratings only | Multiple features |
| **Cold start** | Poor | Better (uses features) |
| **Training** | Fast (~1-2 min) | Slower (~10-30 min) |
| **Inference** | Very fast | Fast |
| **Patterns** | Linear | Non-linear |
| **Interpretability** | Low | Low |

### Lite database (`sputnik_lite.db`)

In lightweight deployments (e.g., PythonAnywhere), the `sputnik_lite.db` database is used. It reuses **release embeddings generated on the full database** to keep quality without retraining the user tower.

- **Embedding source:** the `release_embeddings_dl` table in the lite DB contains ~6,000 items copied from a full run of `offline_recommender/build_two_towers.py` over `data/sputnik.db`.
- **Recommended update flow:**
  1. Run full training on the large DB to refresh item embeddings.
  2. Run `scripts/build_lite_db.py --force`, which copies releases and their embeddings into `sputnik_lite.db`.
  3. Users generate their embedding by pressing the **“Advanced recommendations”** button, which triggers `app/two_towers_update.py`.
- **Production method:** `update_user_embedding()` computes a weighted average of embeddings for releases the user rated positively (`rating ≥ 3`). Weight is `max(0.1, rating / 5.0)`; then it is L2-normalized and stored in `user_embeddings_dl`. This works for any user without depending on an extra model.
- **Lite user tower:** there is an experimental model (not versioned in git) at `models/user_tower_sputnik_lite.keras`, but it only covers ~293 artists and ~500 releases, so it was discarded for production. It can be used for local tests but is not actively maintained.

---

## Co-occurrence (release_pairs)

**Function:** `recommend_from_pairs(user_id, limit=9)`

System based on how often releases co-occur in users’ collections. Ideal for users with few ratings (≤8).

### Building the `release_pairs` table (Offline)

**Script:** `offline_recommender/build_release_pairs.py`

#### Process:

1. **Positive interaction analysis:**
    - Filters interactions with `rating >= 3.0` (configurable)
    - Creates a temporary table with (user, release) for positive ratings

2. **Co-occurrence computation:**
    - For each release pair (A, B), counts how many users rated both
    - Processes in batches for efficiency (default batch_size=250)

3. **Metric computation:**
    - **`pair_count`**: number of users who rated both releases
    - **`jaccard`**: similarity between user sets

      ```
      jaccard = pair_count / (users_A + users_B - pair_count)
      ```

    - **`lift`**: statistical association measure

      ```
      lift = pair_count / (users_A * users_B)
      ```

4. **Filtering:**
    - Stores only pairs with `pair_count >= 3` (configurable with `--min-pair-count`)
    - Table is bidirectional: if (A, B) exists, (B, A) also exists

### Real-time recommendation

#### `_score_pairs()` algorithm:

1. **Get anchors:** releases the user rated positively

2. **Find relations:** find all related releases in `release_pairs`

3. **Compute score per candidate:**

    ```python
    score = rating_weight * recency_weight * pair_count *
              (0.7 + 0.3 * lift) * (0.5 + 0.5 * jaccard)
    ```

    **Components:**
    - **`rating_weight`**: `max(0.1, rating / 5.0)`
    - **`recency_weight`**: `1 / log2(days_since_rating + 1)`
    - **`pair_count`**: co-occurrence frequency
    - **`lift`**: surprise factor (0.7 base + 0.3 * lift)
    - **`jaccard`**: user-set similarity (0.5 base + 0.5 * jaccard)

4. **Accumulate scores:** if a release appears from multiple anchors, scores add up

5. **Sort and filter:** return top N excluding already-seen items

### Practical Example

**User rated:**
- Release A: rating 4.5, 10 days ago
- Release B: rating 3.5, 5 days ago

**In `release_pairs`:**
- (A, X): pair_count=50, lift=2.0, jaccard=0.3
- (B, X): pair_count=30, lift=1.5, jaccard=0.2

**Score computation for X:**

```
From A: (4.5/5) * recency_A * 50 * (0.7 + 0.3*2.0) * (0.5 + 0.5*0.3)
          = 0.9 * 0.85 * 50 * 1.3 * 0.65 ≈ 32.3

From B: (3.5/5) * recency_B * 30 * (0.7 + 0.3*1.5) * (0.5 + 0.5*0.2)
          = 0.7 * 0.92 * 30 * 1.15 * 0.6 ≈ 13.3

Total score for X = 32.3 + 13.3 = 45.6
```

### Advantages

- Works well with few signals
- Finds direct connections between releases
- Considers rating and recency
- Uses statistical metrics (lift, jaccard) to reduce noise

### Limitations

- Depends on `release_pairs` quality (requires periodic rebuild)
- With many signals it can become noisy
- Less general than content-based

### Configuration

- **Activation threshold:** `max_pairs_signals = 8` (co-occurrence for ≤8 ratings)
- **Minimum pair count:** `min_pair_count = 3` (during table build)
- **Minimum rating:** `min_rating = 3.0` (positive interaction threshold)

---

## Content Profiles

**Function:** `recommend_content_based(user_id, limit=9)`

System that builds a user profile based on genres and artists from their favorite releases. Activates automatically when the user has 9 positive ratings, or as a fallback when advanced recommendations are not available for users with more ratings.

### Building the profile (`_user_profile`)

1. **Analyze positive ratings:**
    - Filter interactions with `rating >= 3.0`
    - For each positive interaction:

2. **Compute weights:**
    - **`rating_weight`**: `max(0.1, rating / 5.0)`
    - **`recency_weight`**: `1 / log2(days_since_rating + 1)`
    - **`base_weight`**: `rating_weight * recency_weight`

3. **Distribute weights:**
    - Extract release genres (`release_genres`)
    - Extract release artist (`release_artist_id`)
    - Distribute `base_weight` across genres (split if multiple genres)
    - Assign full `base_weight` to the artist

4. **Result:**

    ```python
    {
         "genres": {genre_id: accumulated_weight, ...},
         "artists": {artist_id: accumulated_weight, ...},
         "total_weight": total
    }
    ```

### Candidate generation

1. **Genre pool:**
    - Fetch all releases belonging to profile genres
    - Limit: `limit * candidate_pool_multiplier` (default: 5×)

2. **Artist pool:**
    - Fetch all releases from profile artists
    - Limit: `limit * candidate_pool_multiplier`

3. **Combine:**
    - Merge both pools (using a `set` to deduplicate)
    - Exclude already-seen/rated releases

### Scoring (`_content_score`)

For each candidate:

```python
score = (genre_weight * genres_weight) +
          (artist_weight * artist_weight_value) +
          (popularity_prior * popularity_score)
```

**Components:**

1. **Genre weight:**

    ```python
    for genre_id in release_genres:
         score += genre_weight * profile["genres"].get(genre_id, 0.0)
    ```

2. **Artist weight:**

    ```python
    if artist_id in profile["artists"]:
         score += artist_weight * profile["artists"][artist_id]
    ```

3. **Popularity prior:**

    ```python
    popularity_score = (
         0.6 * (avg_rating / 5.0) +
         0.3 * log1p(ratings_count) +
         0.1 * recent_bonus
    )
    ```

    Where `recent_bonus` accounts for release age:

    ```python
    years_old = current_year - release_year
    recent_bonus = max(0.0, 1.0 - (years_old / 50.0))
    ```

### Current configuration

- **`genre_weight`**: `1.0`
- **`artist_weight`**: `0.8` (slightly lower than genres)
- **`popularity_prior`**: `0.3`
- **`candidate_pool_multiplier`**: `5`

### Advantages

- Generalizes well with more user data
- Does not depend on specific co-occurrences
- Uses multiple factors (genres, artists, popularity)
- Works better for users with longer histories

### Limitations

- Needs enough ratings to build a robust profile
- Can be biased if user rates only a very specific genre
- Less personalized than co-occurrence for new users

---

## Popularity

**Function:** `_popular_unseen_releases(user_id, limit)`

Fallback strategy that recommends popular releases the user has not seen yet. Used when primary strategies do not produce enough candidates.

### Algorithm

1. **SQL query:**

    ```sql
    SELECT r.id_release
    FROM releases AS r
    LEFT JOIN interactions AS i
         ON i.id_release = r.id_release AND i.id_user = ?
    WHERE i.id_user IS NULL
    ORDER BY
         (r.ratings_count IS NULL),
         r.ratings_count DESC,
         (r.avg_rating IS NULL),
         r.avg_rating DESC,
         r.id_release DESC
    LIMIT ?;
    ```

2. **Ordering:**
    - First by `ratings_count` (more ratings = more popular)
    - Then by `avg_rating` (higher average rating)
    - Excludes releases already interacted with

### Usage

Activates automatically as fallback when:
- Primary strategies do not produce enough candidates
- User has no positive ratings

### Advantages

- Simple and fast
- Works for new users
- Ensures recommendations are always available

### Limitations

- Not personalized
- Can recommend very well-known releases the user already knows
- Does not consider user preferences

---

## Random Exploration

**Function:** `recommend_random(user_id, limit=9)`

Strategy that selects random releases from the catalog to promote exploration and diversity.

### Algorithm

1. **SQL query:**

    ```sql
    SELECT r.id_release
    FROM releases AS r
    WHERE NOT EXISTS (
         SELECT 1
         FROM interactions AS i
         WHERE i.id_release = r.id_release AND i.id_user = ?
    );
    ```

2. **Random selection:**
    - Fetches all unseen releases
    - Uses `random.sample()` to pick `limit` random releases

### Usage

Activates as a last fallback when:
- Main strategies do not generate enough candidates
- Diversity is needed

### Advantages

- Encourages exploration
- Surfaces releases outside the user’s usual zone
- Increases recommendation diversity

### Limitations

- Does not consider user preferences
- Can recommend low-quality releases
- Lower expected relevance

---

## Contextual Recommendations

**Function:** `recommend_context(user_id, release_id, limit=3)`

Specialized system to recommend releases related to a specific release. Used on release detail pages.

### Multi-source Strategy

1. **Direct recommendations (`release_recommendations`):**
    - Fetches from the precomputed recommendations table
    - Sorts by popularity (`ratings_count`, `avg_rating`)

2. **Co-occurrences (`release_pairs`):**
    - If candidates are missing, queries `release_pairs`
    - Sorts by descending `pair_count`

3. **Artist discography:**
    - If still missing, fetches other releases by the same artist
    - Sorts by release year (most recent first)

4. **Popularity fallback:**
    - If still missing, fills with popular releases

### Filtering

- Excludes releases already seen/rated by the user
- Excludes the current release (`release_id`)
- Deduplicates

### Advantages

- Multiple information sources
- Specialized for a specific context
- Uses direct and indirect relations

### Limitations

- Depends on relation table quality
- Can be limited if the release has few relationships

---

## Evaluation Metrics

**Module:** `app/metrics.py`

The system implements standard ranking-based recommendation evaluation metrics.

### DCG (Discounted Cumulative Gain)

**Function:** `discounted_cumulative_gain(relevance_scores)`

Measures ranking quality while accounting for the position of relevant items.

```python
DCG = Σ(relevance_i / log2(i + 2))
```

- Items in higher positions have more weight
- Discount increases logarithmically with position

### IDCG (Ideal Discounted Cumulative Gain)

**Function:** `ideal_discounted_cumulative_gain(relevance_scores)`

DCG of the ideal ranking (items sorted by descending relevance).

### NDCG (Normalized Discounted Cumulative Gain)

**Function:** `normalized_discounted_cumulative_gain(relevance_scores)`

Normalizes DCG by IDCG to produce a value between 0 and 1.

```python
NDCG = DCG / IDCG
```

- **1.0**: perfect ranking
- **0.0**: no relevant items (or worse than random)

### Precision@k, Recall@k, and F1@k

**Functions:** `precision_at_k()`, `recall_at_k()`, `f1_at_k()`

Classic information-retrieval metrics:

- **Precision@k**: fraction of relevant recommendations in top-k
- **Recall@k**: fraction of relevant items retrieved in top-k
- **F1@k**: harmonic mean of Precision@k and Recall@k

### MRR (Mean Reciprocal Rank)

**Function:** `mean_reciprocal_rank(recommended, relevant)`

Inverse rank of the first relevant item.

### Diversity and novelty metrics

- **Genre Diversity**: fraction of unique genres in recommendations
- **Artist Diversity**: fraction of unique artists in recommendations
- **Novelty**: average -log2(popularity) of recommended items (less popular = more novel)
- **Coverage**: fraction of catalog that can be recommended (aggregate across users)

### Offline evaluation

**Script:** `offline_recommender/evaluate_recommender.py`

Evaluates recommendation systems using interaction holdouts:

1. **Select users:** users with at least `min_ratings` ratings
2. **Split data:** holds out `holdout_ratio` of interactions for testing
3. **Evaluate:** computes NDCG@k for each strategy:
    - Max-Ensemble
    - RRF-Ensemble
    - Hybrid
    - Advanced (NMF + Two Towers)
    - NMF
    - Two Towers
    - Pairs
    - Content-based
    - Random
    - Popular
4. **Report:** generates CSV with per-user detailed results

### Usage

```bash
python -m offline_recommender.evaluate_recommender \
     --min-ratings 50 \
     --sample-size 100 \
     --holdout-ratio 0.2 \
     --k 9 \
     --output eval_results.csv
```

---

## Global Configuration

**Class:** `Config` in `app/recommender.py`

### Main parameters

- **`positive_rating_threshold`**: `3.0` - minimum rating to consider an interaction positive
- **`max_pairs_signals`**: `8` - threshold to switch from co-occurrence to content
- **`min_advanced_level_1_signals`**: `20` - threshold for advanced level 1 (NMF)
- **`min_advanced_level_2_signals`**: `30` - threshold for advanced level 2 (NMF + Two Towers)
- **Dynamic weights at level 2**: 50/50 → 40/60 → 30/70 → 20/80 as ratings increase (progressively favors Two Towers)
- **`advanced_consensus_bonus`**: `0.2` - bonus for candidates present in both systems
- **`min_two_towers_signals`**: `10` - legacy threshold (kept for compatibility)
- **`min_nmf_signals`**: `20` - legacy threshold (kept for compatibility)
- **`genre_weight`**: `1.0` - weight of genres in content scoring
- **`artist_weight`**: `0.8` - weight of artists in content scoring
- **`popularity_prior`**: `0.3` - weight of popularity factor
- **`recency_log_base`**: `2.0` - log base used for recency
- **`popularity_recent_divisor`**: `50.0` - divisor used for recency bonus
- **`pairs_limit_multiplier`**: `3` - multiplier for pair limits
- **`pairs_table_sample`**: `10` - pairs table sample size
- **`candidate_pool_multiplier`**: `5` - candidate pool multiplier

---

## Strategy Summary

| Strategy | When it is used | Personalization | Complexity |
|------------|-----------------|-----------------|------------|
| **Max-Ensemble** | Stable | Very High | Medium |
| **RRF-Ensemble** | Stable | Very High | Medium |
| **Hybrid Engine** | Always (main) | High | High |
| **Co-occurrence** | ≤8 ratings | Medium-High | Medium |
| **Advanced Recommendations** | ≥20 ratings | Very High | High |
| **NMF** | Advanced level 1 (20-29) | Very High | High |
| **Two Towers** | Advanced level 2 (≥30) | Very High | High |
| **Content** | 9 ratings, fallback | High | Medium |
| **Popularity** | Fallback | Low | Low |
| **Random** | Last fallback | None | Low |
| **Contextual** | Release page | Medium | Medium |

---

## Future Strategies

### User-Based Collaborative Filtering (User-Based CF)

**Status**: Not implemented due to increased DB weight and because it did not add more signal than item-based CF; kept as a future consideration.

The current system uses item-based collaborative filtering via the `release_pairs` table. A possible extension would be user-based collaborative filtering.

#### Concept

Instead of finding releases similar to what the user rated (item-based), user-based CF finds users similar to the target user and recommends releases those similar users rated positively.

#### Potential advantages

- **More diverse discovery**: can cross genres and find indirect relationships
- **Better for long-history users**: leverages preferences of similar users with more data
- **Adaptation to taste changes**: periodic similarity recomputation can reflect preference shifts

#### Drawbacks and considerations

- **Scalability**: requires computing and maintaining user–user similarities (O(n²) matrix)
- **Overlap dependence**: needs enough overlap to find useful neighbors
- **Cold start**: does not work well for new users without enough history
- **Operational complexity**: requires periodic maintenance and caching strategies

#### When to consider implementation

User-based CF would be beneficial if:
- There is enough average overlap between users (≥15-20% Jaccard similarity)
- There is a significant number of long-history users (≥20-50 positive ratings)
- The user–item matrix density is sufficient to find useful neighbors
- Infrastructure exists to precompute similarities offline

#### Suggested implementation (if adopted)

1. **Offline precomputation**: similar to `release_pairs`, build a `user_similarities` table with:
    - cosine or Pearson similarity between user rating vectors
    - top-K most similar neighbors per user
    - only for users with enough history (≥20 positive ratings)

2. **Hybrid strategy**:
    - use user-based CF only for users with ≥50 positive ratings
    - keep item-based as the main strategy for the rest
    - combine both signals for very long histories

3. **Analysis script**: `offline_recommender/analyze_user_cf_potential.py` can evaluate whether implementation would be beneficial

#### Technical references

- Algorithm: find K most similar users using cosine/Pearson similarity over centered ratings
- Optimization: precompute offline, cache in memory, update periodically
- Evaluation: compare NDCG@k with the current system before deploying

---

## References

- Implementation: `app/recommender.py`
- Max-Ensemble: `recommend_max_ensemble()` - combines strategies via max score
- RRF-Ensemble: `recommend_rrf_ensemble()` - combines strategies via agreement (RRF)
- Advanced recommendations: `recommend_advanced()` - combines NMF + Two Towers
- On-demand embedding updates: `app/nmf_update.py`, `app/two_towers_update.py`
- Unified endpoint: `POST /actualizar-recomendaciones-avanzadas` in `app/app.py`
- Ensemble endpoints: `/api/recommend/<user_id>/max_ensemble`, `/api/recommend/<user_id>/rrf_ensemble` in `app/app.py`
- Pair builder: `offline_recommender/build_release_pairs.py`
- NMF embedding builder: `offline_recommender/build_nmf_embeddings.py`
- Two Towers embedding builder: `offline_recommender/build_two_towers.py`
- Evaluation: `offline_recommender/evaluate_recommender.py`
- Metrics: `app/metrics.py`
- Evaluation analysis: `notebooks/analisis_evaluacion_recomendaciones.ipynb`
