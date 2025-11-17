"""Precompute NMF embeddings for users and releases.

This script trains a Non-negative Matrix Factorization model on user-item interactions
and stores the resulting embeddings in the database for fast recommendation inference.

The script supports three modes:
1. Standard training: Train with specified hyperparameters
2. Hyperparameter optimization: Use Bayesian optimization to find optimal hyperparameters
3. Load parameters: Load previously optimized hyperparameters from JSON file

Examples:
    # Standard training with default parameters
    python -m offline_recommender.build_nmf_embeddings

    # Optimize hyperparameters only (saves to models/NMF/nmf_params.json by default)
    python -m offline_recommender.build_nmf_embeddings \\
        --optimize --n-calls 30 --save-params

    # Optimize with checkpointing (can resume if interrupted)
    python -m offline_recommender.build_nmf_embeddings \\
        --optimize --n-calls 30 --checkpoint-dir checkpoints/nmf \\
        --save-params

    # Resume interrupted optimization
    python -m offline_recommender.build_nmf_embeddings \\
        --optimize --n-calls 30 --resume-from checkpoints/nmf/nmf_optimization_checkpoint.pkl \\
        --checkpoint-dir checkpoints/nmf --save-params

    # Optimize and train with best parameters (auto-saves to models/NMF/nmf_params.json)
    python -m offline_recommender.build_nmf_embeddings \\
        --optimize-and-train --n-calls 30

    # Train with previously optimized parameters (searches in models/NMF/ if relative path)
    python -m offline_recommender.build_nmf_embeddings \\
        --load-params nmf_params.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path
from time import perf_counter
from typing import Dict
from typing import List
from typing import Tuple
from typing import Union

import numpy as np
from scipy import sparse
from sklearn.decomposition import NMF
from sklearn.model_selection import train_test_split


try:
    from skopt import dump
    from skopt import gp_minimize
    from skopt import load
    from skopt.space import Integer
    from skopt.space import Real
    from skopt.utils import create_result
    from skopt.utils import use_named_args

    SKOPT_AVAILABLE = True
except ImportError:
    SKOPT_AVAILABLE = False
    dump = None
    load = None
    create_result = None

try:
    from app import metrics

    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False


LOGGER = logging.getLogger("build_nmf_embeddings")


def resolve_database_path(database: str | None) -> Path:
    if database:
        return Path(database).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "data" / "sputnik.db"


def load_user_item_matrix(
    connection: sqlite3.Connection,
    min_rating: float = 3.0,
    min_user_ratings: int = 5,
    min_release_ratings: int = 3,
) -> Tuple[sparse.csr_matrix, List[str], List[int], Dict[str, int], Dict[int, int]]:
    """Load user-item interaction matrix from database.

    Args:
        min_user_ratings: Minimum positive ratings per user to include
        min_release_ratings: Minimum positive ratings per release to include

    Returns:
        Tuple of:
        - Matrix (users x items) with ratings
        - List of user IDs in order
        - List of release IDs in order
        - Mapping: user_id -> row index
        - Mapping: release_id -> column index
    """
    LOGGER.info(
        "Loading user-item interactions "
        "(min_rating=%.2f, min_user_ratings=%d, min_release_ratings=%d)...",
        min_rating,
        min_user_ratings,
        min_release_ratings,
    )
    start = perf_counter()

    # Filter users and releases first to reduce matrix size
    LOGGER.info("Filtering users and releases...")
    user_filter_query = """
        SELECT id_user
        FROM interactions
        WHERE rating >= ?
        GROUP BY id_user
        HAVING COUNT(*) >= ?
    """
    eligible_users = {
        row[0] for row in connection.execute(user_filter_query, (min_rating, min_user_ratings))
    }
    LOGGER.info("Found %d eligible users", len(eligible_users))

    release_filter_query = """
        SELECT id_release
        FROM interactions
        WHERE rating >= ?
        GROUP BY id_release
        HAVING COUNT(*) >= ?
    """
    eligible_releases = {
        row[0]
        for row in connection.execute(release_filter_query, (min_rating, min_release_ratings))
    }
    LOGGER.info("Found %d eligible releases", len(eligible_releases))

    if not eligible_users or not eligible_releases:
        raise ValueError("No eligible users or releases found with given filters")

    # Use temporary tables to avoid SQL parameter limits
    LOGGER.info("Creating temporary filter tables...")
    connection.execute("DROP TABLE IF EXISTS temp_eligible_users;")
    connection.execute(
        "CREATE TEMP TABLE temp_eligible_users (id_user TEXT PRIMARY KEY) WITHOUT ROWID;"
    )
    connection.executemany(
        "INSERT INTO temp_eligible_users (id_user) VALUES (?);",
        [(uid,) for uid in eligible_users],
    )

    connection.execute("DROP TABLE IF EXISTS temp_eligible_releases;")
    connection.execute(
        "CREATE TEMP TABLE temp_eligible_releases (id_release INTEGER PRIMARY KEY) WITHOUT ROWID;"
    )
    connection.executemany(
        "INSERT INTO temp_eligible_releases (id_release) VALUES (?);",
        [(rid,) for rid in eligible_releases],
    )
    connection.commit()

    # Load positive interactions only for eligible users/releases
    query = """
        SELECT i.id_user, i.id_release, i.rating
        FROM interactions i
        INNER JOIN temp_eligible_users u ON u.id_user = i.id_user
        INNER JOIN temp_eligible_releases r ON r.id_release = i.id_release
        WHERE i.rating >= ?
        ORDER BY i.id_user, i.id_release
    """

    rows = connection.execute(query, (min_rating,)).fetchall()

    if not rows:
        raise ValueError("No interactions found with rating >= %.2f" % min_rating)

    # Build mappings
    user_ids = sorted(set(row["id_user"] for row in rows))
    release_ids = sorted(set(row["id_release"] for row in rows))

    user_to_idx = {user_id: idx for idx, user_id in enumerate(user_ids)}
    release_to_idx = {release_id: idx for idx, release_id in enumerate(release_ids)}

    # Build sparse matrix (CSR format for efficient NMF)
    LOGGER.info("Building sparse matrix...")
    import gc

    # Process in chunks to avoid memory spikes
    chunk_size = 1000000  # Process 1M rows at a time
    user_indices = []
    release_indices = []
    ratings = []

    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        for row in chunk:
            user_idx = user_to_idx[row["id_user"]]
            release_idx = release_to_idx[row["id_release"]]
            user_indices.append(user_idx)
            release_indices.append(release_idx)
            ratings.append(float(row["rating"]))

        # Periodic garbage collection for large datasets
        if i > 0 and i % (chunk_size * 5) == 0:
            gc.collect()
            LOGGER.debug("Processed %d/%d rows, memory cleanup", i, len(rows))

    # Free row data before building matrix
    del rows
    gc.collect()

    matrix = sparse.csr_matrix(
        (ratings, (user_indices, release_indices)),
        shape=(len(user_ids), len(release_ids)),
        dtype=np.float32,
    )

    # Free intermediate lists
    del user_indices, release_indices, ratings
    gc.collect()

    elapsed = perf_counter() - start
    density = matrix.nnz / (len(user_ids) * len(release_ids))
    memory_mb = (matrix.data.nbytes + matrix.indices.nbytes + matrix.indptr.nbytes) / 1024**2
    LOGGER.info(
        "Loaded sparse matrix: %d users × %d releases, density=%.4f%%, "
        "nnz=%d, memory=%.1f MB (%.2fs)",
        len(user_ids),
        len(release_ids),
        density * 100,
        matrix.nnz,
        memory_mb,
        elapsed,
    )

    return matrix, user_ids, release_ids, user_to_idx, release_to_idx


def train_nmf(
    matrix: np.ndarray,
    n_components: int = 50,
    max_iter: int = 200,
    random_state: int = 42,
    alpha_W: float = 0.001,
    alpha_H: float = 0.001,
    l1_ratio: float = 0.0,
    return_model: bool = False,
) -> Union[Tuple[np.ndarray, np.ndarray], Tuple[np.ndarray, np.ndarray, NMF]]:
    """Train NMF model on user-item matrix.

    Args:
        matrix: User-item interaction matrix
        n_components: Number of latent factors
        max_iter: Maximum iterations
        random_state: Random seed
        alpha_W: L2 regularization for user embeddings (W matrix). Default 0.001 (reduced from 0.01)
        alpha_H: L2 regularization for item embeddings (H matrix). Default 0.001 (reduced from 0.01)
        l1_ratio: Mix of L1/L2 regularization. Default 0.0 (no L1, was 0.1)
        return_model: If True, also return the trained model

    Returns:
        Tuple of (user_embeddings, item_embeddings) or (user_embeddings, item_embeddings, model)
        - user_embeddings: (n_users, n_components)
        - item_embeddings: (n_items, n_components)
        - model: Trained NMF model (if return_model=True)
    """
    LOGGER.info(
        "Training NMF model (n_components=%d, max_iter=%d, "
        "alpha_W=%.4f, alpha_H=%.4f, l1_ratio=%.2f)...",
        n_components,
        max_iter,
        alpha_W,
        alpha_H,
        l1_ratio,
    )
    start = perf_counter()

    # Use alpha_W and alpha_H for newer sklearn versions, fallback to alpha for older
    try:
        model = NMF(
            n_components=n_components,
            max_iter=max_iter,
            random_state=random_state,
            alpha_W=alpha_W,  # L2 regularization for W (users) - REDUCED from 0.01
            alpha_H=alpha_H,  # L2 regularization for H (items) - REDUCED from 0.01
            l1_ratio=l1_ratio,  # Mix of L1/L2 regularization - REDUCED from 0.1 to 0.0
            verbose=1 if LOGGER.isEnabledFor(logging.DEBUG) else 0,
        )
    except TypeError:
        # Fallback for older sklearn versions
        # Calculate equivalent alpha for older API
        alpha_combined = (alpha_W + alpha_H) / 2.0
        model = NMF(
            n_components=n_components,
            max_iter=max_iter,
            random_state=random_state,
            alpha=alpha_combined,  # L2 regularization
            l1_ratio=l1_ratio,  # Mix of L1/L2 regularization
            verbose=1 if LOGGER.isEnabledFor(logging.DEBUG) else 0,
        )

    # NMF factorizes matrix as: matrix ≈ user_embeddings @ item_embeddings.T
    # So we get: W (users x components) and H (components x items)
    W = model.fit_transform(matrix)  # User embeddings
    H = model.components_  # Item embeddings (transposed)

    elapsed = perf_counter() - start
    reconstruction_error = model.reconstruction_err_

    LOGGER.info(
        "NMF training completed in %.2fs (reconstruction_error=%.4f)",
        elapsed,
        reconstruction_error,
    )

    if return_model:
        return W, H.T, model  # Return item_embeddings as (n_items, n_components)
    return W, H.T  # Return item_embeddings as (n_items, n_components)


def evaluate_nmf_mse(
    train_matrix: sparse.csr_matrix,
    test_matrix: sparse.csr_matrix,
    n_components: int,
    max_iter: int,
    random_state: int,
    alpha_W: float,
    alpha_H: float,
    l1_ratio: float,
) -> float:
    """Evaluate NMF model using reconstruction error (MSE) on test set.

    Args:
        train_matrix: Training user-item matrix
        test_matrix: Test user-item matrix (same users/items as train)
        n_components: Number of latent factors
        max_iter: Maximum iterations
        random_state: Random seed
        alpha_W: L2 regularization for user embeddings
        alpha_H: L2 regularization for item embeddings
        l1_ratio: Mix of L1/L2 regularization

    Returns:
        Negative reconstruction error (for minimization in optimization)
    """
    # Train model on training data
    _, _, model = train_nmf(
        train_matrix,
        n_components=n_components,
        max_iter=max_iter,
        random_state=random_state,
        alpha_W=alpha_W,
        alpha_H=alpha_H,
        l1_ratio=l1_ratio,
        return_model=True,
    )

    # Transform test data using trained model
    W_test = model.transform(test_matrix)
    H_test = model.components_

    # Compute reconstruction error on test set
    # Use sparse operations to avoid converting entire matrix to dense
    reconstructed = W_test @ H_test
    # For sparse matrices, compute MSE only on non-zero elements
    if sparse.issparse(test_matrix):
        # Get non-zero elements
        test_coo = test_matrix.tocoo()
        reconstructed_values = reconstructed[test_coo.row, test_coo.col]
        error = np.mean((test_coo.data - reconstructed_values) ** 2)
    else:
        error = np.mean((test_matrix - reconstructed) ** 2)

    return -error  # Return negative for minimization


def evaluate_nmf_ndcg(
    train_matrix: sparse.csr_matrix,
    test_matrix: sparse.csr_matrix,
    user_ids: List[str],
    release_ids: List[int],
    n_components: int,
    max_iter: int,
    random_state: int,
    alpha_W: float,
    alpha_H: float,
    l1_ratio: float,
    k: int = 9,
    min_test_items: int = 1,
) -> float:
    """Evaluate NMF model using NDCG@k on test set.

    This metric evaluates recommendation quality directly, which is more aligned
    with the goal of generating good recommendations than MSE.

    Args:
        train_matrix: Training user-item matrix
        test_matrix: Test user-item matrix (holdout interactions)
        user_ids: List of user IDs corresponding to matrix rows
        release_ids: List of release IDs corresponding to matrix columns
        n_components: Number of latent factors
        max_iter: Maximum iterations
        random_state: Random seed
        alpha_W: L2 regularization for user embeddings
        alpha_H: L2 regularization for item embeddings
        l1_ratio: Mix of L1/L2 regularization
        k: Number of recommendations to evaluate (default: 9)
        min_test_items: Minimum test items per user to include in evaluation

    Returns:
        Average NDCG@k across users (for maximization in optimization)
    """
    if not METRICS_AVAILABLE:
        raise ImportError(
            "app.metrics is required for NDCG evaluation. Make sure the app module is available."
        )

    # Train model on training data
    user_embeddings, item_embeddings, _ = train_nmf(
        train_matrix,
        n_components=n_components,
        max_iter=max_iter,
        random_state=random_state,
        alpha_W=alpha_W,
        alpha_H=alpha_H,
        l1_ratio=l1_ratio,
        return_model=True,
    )

    # Convert test matrix to COO format for efficient iteration
    test_coo = test_matrix.tocoo()

    # Build test sets per user: {user_idx: set(release_idxs)}
    user_test_items: Dict[int, set[int]] = {}
    for i, j, _ in zip(test_coo.row, test_coo.col, test_coo.data, strict=False):
        if i not in user_test_items:
            user_test_items[i] = set()
        user_test_items[i].add(j)

    # Filter users with enough test items
    valid_users = [
        user_idx
        for user_idx, test_items in user_test_items.items()
        if len(test_items) >= min_test_items
    ]

    if not valid_users:
        LOGGER.warning("No users with enough test items for NDCG evaluation")
        return 0.0

    # Evaluate NDCG@k for each user
    ndcg_scores = []
    for user_idx in valid_users:
        # Get user embedding
        user_emb = user_embeddings[user_idx]
        user_norm = np.linalg.norm(user_emb)

        if user_norm == 0:
            continue

        # Get test items for this user
        test_item_indices = user_test_items[user_idx]

        # Calculate cosine similarities with all items
        # Only consider items not in training set for this user
        train_items = set(train_matrix[user_idx].indices)
        candidate_items = [
            item_idx for item_idx in range(len(release_ids)) if item_idx not in train_items
        ]

        if not candidate_items:
            continue

        # Calculate similarities
        similarities = {}
        for item_idx in candidate_items:
            item_emb = item_embeddings[item_idx]
            item_norm = np.linalg.norm(item_emb)
            if item_norm > 0:
                similarity = np.dot(user_emb, item_emb) / (user_norm * item_norm)
                similarities[item_idx] = float(similarity)

        # Get top-k recommendations
        ranked_items = sorted(similarities.items(), key=lambda x: x[1], reverse=True)
        top_k_indices = [item_idx for item_idx, _ in ranked_items[:k]]

        # Calculate relevance scores (1.0 if in test set, 0.0 otherwise)
        relevance_scores = [
            1.0 if item_idx in test_item_indices else 0.0 for item_idx in top_k_indices
        ]

        # Calculate NDCG@k
        ndcg = metrics.normalized_discounted_cumulative_gain(relevance_scores)
        ndcg_scores.append(ndcg)

    if not ndcg_scores:
        return 0.0

    # Return average NDCG@k
    avg_ndcg = sum(ndcg_scores) / len(ndcg_scores)
    return avg_ndcg


def evaluate_nmf(
    train_matrix: sparse.csr_matrix,
    test_matrix: sparse.csr_matrix,
    user_ids: List[str] | None = None,
    release_ids: List[int] | None = None,
    n_components: int = 50,
    max_iter: int = 200,
    random_state: int = 42,
    alpha_W: float = 0.001,
    alpha_H: float = 0.001,
    l1_ratio: float = 0.0,
    metric: str = "mse",
    k: int = 9,
) -> float:
    """Evaluate NMF model using specified metric.

    Args:
        train_matrix: Training user-item matrix
        test_matrix: Test user-item matrix
        user_ids: List of user IDs (required for NDCG metric)
        release_ids: List of release IDs (required for NDCG metric)
        n_components: Number of latent factors
        max_iter: Maximum iterations
        random_state: Random seed
        alpha_W: L2 regularization for user embeddings
        alpha_H: L2 regularization for item embeddings
        l1_ratio: Mix of L1/L2 regularization
        metric: Evaluation metric ("mse" or "ndcg")
        k: Number of recommendations for NDCG@k (default: 9)

    Returns:
        Score to maximize (negative MSE for mse, NDCG@k for ndcg)
    """
    if metric == "mse":
        return evaluate_nmf_mse(
            train_matrix=train_matrix,
            test_matrix=test_matrix,
            n_components=n_components,
            max_iter=max_iter,
            random_state=random_state,
            alpha_W=alpha_W,
            alpha_H=alpha_H,
            l1_ratio=l1_ratio,
        )
    elif metric == "ndcg":
        if user_ids is None or release_ids is None:
            raise ValueError("user_ids and release_ids are required for NDCG metric")
        ndcg_score = evaluate_nmf_ndcg(
            train_matrix=train_matrix,
            test_matrix=test_matrix,
            user_ids=user_ids,
            release_ids=release_ids,
            n_components=n_components,
            max_iter=max_iter,
            random_state=random_state,
            alpha_W=alpha_W,
            alpha_H=alpha_H,
            l1_ratio=l1_ratio,
            k=k,
        )
        return ndcg_score  # NDCG is already a score to maximize
    else:
        raise ValueError(f"Unknown metric: {metric}. Use 'mse' or 'ndcg'")


def optimize_hyperparameters(
    matrix: sparse.csr_matrix,
    user_ids: List[str],
    release_ids: List[int],
    n_calls: int = 20,
    random_state: int = 42,
    test_size: float = 0.2,
    metric: str = "ndcg",
    k: int = 9,
    checkpoint_dir: Path | None = None,
    resume_from: Path | None = None,
) -> Dict[str, float | int]:
    """Optimize NMF hyperparameters using Bayesian optimization.

    Args:
        matrix: User-item interaction matrix
        user_ids: List of user IDs corresponding to matrix rows
        release_ids: List of release IDs corresponding to matrix columns
        n_calls: Number of optimization iterations
        random_state: Random seed
        test_size: Fraction of data to use for testing
        metric: Evaluation metric ("mse" or "ndcg"). Default "ndcg" (recommended)
        k: Number of recommendations for NDCG@k (default: 9)
        checkpoint_dir: Directory to save checkpoints (if None, no checkpoints)
        resume_from: Path to checkpoint file to resume from (if None, start fresh)

    Returns:
        Dictionary with optimal hyperparameters
    """
    if not SKOPT_AVAILABLE:
        raise ImportError(
            "scikit-optimize is required for hyperparameter optimization. "
            "Install it with: pip install scikit-optimize"
        )

    # Check if resuming from checkpoint
    previous_result = None
    # n_initial_points must be <= n_calls, and gp_minimize requires at least 5 calls
    n_initial_points = min(5, n_calls)
    if resume_from:
        if not SKOPT_AVAILABLE or load is None:
            raise ImportError(
                "scikit-optimize is required for checkpoint resume. "
                "Install it with: pip install scikit-optimize"
            )
        checkpoint_path = Path(resume_from).expanduser().resolve()
        if not checkpoint_path.exists():
            raise FileNotFoundError(f"Checkpoint file not found: {checkpoint_path}")

        LOGGER.info("Loading checkpoint from %s...", checkpoint_path)
        previous_result = load(str(checkpoint_path))

        # Calculate how many evaluations were already done
        n_completed = len(previous_result.func_vals)
        remaining_calls = n_calls - n_completed

        if remaining_calls <= 0:
            LOGGER.warning(
                "Checkpoint already has %d evaluations (requested %d). "
                "Using best parameters from checkpoint.",
                n_completed,
                n_calls,
            )
            # Extract best parameters from checkpoint
            best_params = {
                "n_components": int(previous_result.x[0]),
                "max_iter": int(previous_result.x[1]),
                "alpha_W": float(previous_result.x[2]),
                "alpha_H": float(previous_result.x[3]),
                "l1_ratio": float(previous_result.x[4]),
            }
            best_score = -previous_result.fun
            if metric == "mse":
                best_score = -best_score

            LOGGER.info(
                "Using checkpoint results. Best parameters: %s (%s=%.6f)",
                best_params,
                metric.upper(),
                best_score,
            )
            return best_params

        LOGGER.info(
            "Resuming optimization: %d evaluations completed, %d remaining",
            n_completed,
            remaining_calls,
        )
        n_calls = remaining_calls
        n_initial_points = 0  # Don't need initial points when resuming
        # Ensure we have at least 1 call remaining
        if n_calls < 1:
            n_calls = 1

    LOGGER.info(
        "Starting Bayesian hyperparameter optimization (%d iterations, metric=%s)...",
        n_calls if not resume_from else n_calls + len(previous_result.func_vals),
        metric,
    )

    # Split data for evaluation
    # For sparse matrices, we need to split by users
    n_users = matrix.shape[0]
    indices = np.arange(n_users)
    train_indices, test_indices = train_test_split(
        indices, test_size=test_size, random_state=random_state
    )

    train_matrix = matrix[train_indices, :]
    test_matrix = matrix[test_indices, :]

    # Split user_ids accordingly
    train_user_ids = [user_ids[i] for i in train_indices]

    LOGGER.info(
        "Split data: %d train users, %d test users",
        len(train_indices),
        len(test_indices),
    )

    # Define search space
    dimensions = [
        Integer(10, 100, name="n_components"),
        Integer(50, 500, name="max_iter"),
        Real(1e-5, 1e-1, prior="log-uniform", name="alpha_W"),
        Real(1e-5, 1e-1, prior="log-uniform", name="alpha_H"),
        Real(0.0, 1.0, name="l1_ratio"),
    ]

    # Objective function
    @use_named_args(dimensions=dimensions)
    def objective(n_components, max_iter, alpha_W, alpha_H, l1_ratio):
        try:
            score = evaluate_nmf(
                train_matrix=train_matrix,
                test_matrix=test_matrix,
                user_ids=train_user_ids if metric == "ndcg" else None,
                release_ids=release_ids if metric == "ndcg" else None,
                n_components=n_components,
                max_iter=max_iter,
                random_state=random_state,
                alpha_W=alpha_W,
                alpha_H=alpha_H,
                l1_ratio=l1_ratio,
                metric=metric,
                k=k,
            )
            # For MSE, score is negative (we want to minimize error = maximize negative error)
            # For NDCG, score is already positive (we want to maximize it)
            # gp_minimize minimizes, so we need to negate for maximization
            if metric == "mse":
                # score is already negative MSE, so -score is positive
                # (minimize -score = maximize score)
                objective_value = -score
            else:  # ndcg
                # score is NDCG (positive), we want to maximize it, so minimize -score
                objective_value = -score

            LOGGER.debug(
                "Trial: n_components=%d, max_iter=%d, alpha_W=%.6f, alpha_H=%.6f, "
                "l1_ratio=%.3f, %s=%.6f",
                n_components,
                max_iter,
                alpha_W,
                alpha_H,
                l1_ratio,
                metric.upper(),
                score if metric == "ndcg" else -score,
            )
            return objective_value
        except Exception as e:
            LOGGER.warning("Error in optimization trial: %s", e)
            return 1e6  # Return large error for failed trials

    # Callback to save checkpoint after each iteration
    checkpoint_callback = None
    if checkpoint_dir:
        checkpoint_dir = Path(checkpoint_dir).expanduser().resolve()
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_file = checkpoint_dir / "nmf_optimization_checkpoint.pkl"

        # Calculate total expected calls for logging
        total_expected_calls = n_calls + (len(previous_result.func_vals) if previous_result else 0)

        # Store previous_result in closure for callback
        prev_result_for_callback = previous_result

        def checkpoint_callback(res):
            """Save checkpoint after each iteration."""
            try:
                # Merge with previous results if resuming
                if prev_result_for_callback:
                    merged_x_iters = list(prev_result_for_callback.x_iters) + list(res.x_iters)
                    merged_func_vals = list(prev_result_for_callback.func_vals) + list(
                        res.func_vals
                    )
                    # Update best
                    best_idx = np.argmin(merged_func_vals)
                    best_x = merged_x_iters[best_idx]
                    best_fun = merged_func_vals[best_idx]
                else:
                    merged_x_iters = list(res.x_iters)
                    merged_func_vals = list(res.func_vals)
                    best_x = res.x
                    best_fun = res.fun

                # Create a minimal result object with only serializable data
                # This avoids pickling nested functions
                # create_result uses Xi and yi, not x_iters and func_vals
                checkpoint_result = create_result(
                    Xi=merged_x_iters,
                    yi=merged_func_vals,
                    space=dimensions,
                    models=res.models if hasattr(res, "models") else None,
                    rng=random_state,
                )
                checkpoint_result.x = best_x
                checkpoint_result.fun = best_fun

                dump(checkpoint_result, str(checkpoint_file))
                current_total = len(merged_func_vals)
                LOGGER.info(
                    "Checkpoint saved: %d/%d evaluations completed",
                    current_total,
                    total_expected_calls,
                )
            except Exception as e:
                LOGGER.warning("Failed to save checkpoint: %s", e)

    # Run optimization
    result = gp_minimize(
        func=objective,
        dimensions=dimensions,
        n_calls=n_calls,
        random_state=random_state,
        n_initial_points=n_initial_points,
        acq_func="EI",  # Expected Improvement
        x0=previous_result.x_iters if previous_result else None,
        y0=previous_result.func_vals if previous_result else None,
        callback=checkpoint_callback,
    )

    # If resuming, merge results
    if previous_result:
        # Combine previous and new results
        # Note: gp_minimize doesn't directly support merging, but we can use the best from combined
        all_x_iters = list(previous_result.x_iters) + list(result.x_iters)
        all_func_vals = list(previous_result.func_vals) + list(result.func_vals)

        # Find best overall
        best_idx = np.argmin(all_func_vals)
        best_x = all_x_iters[best_idx]
        best_fun = all_func_vals[best_idx]

        # Create a result-like object with best overall
        result.x = best_x
        result.fun = best_fun
        result.x_iters = all_x_iters
        result.func_vals = all_func_vals

        LOGGER.info(
            "Merged checkpoint results: %d previous + %d new = %d total evaluations",
            len(previous_result.func_vals),
            len(result.func_vals),
            len(all_func_vals),
        )

    # Extract best parameters
    best_params = {
        "n_components": int(result.x[0]),
        "max_iter": int(result.x[1]),
        "alpha_W": float(result.x[2]),
        "alpha_H": float(result.x[3]),
        "l1_ratio": float(result.x[4]),
    }

    # Calculate best score (negate because gp_minimize minimizes)
    best_score = -result.fun
    if metric == "mse":
        best_score = -best_score  # Convert back to MSE (positive)

    LOGGER.info(
        "Optimization completed. Best parameters: %s (%s=%.6f)",
        best_params,
        metric.upper(),
        best_score,
    )

    return best_params


def save_hyperparameters(params: Dict[str, float | int], output_path: Path) -> None:
    """Save hyperparameters to JSON file."""
    with output_path.open("w") as f:
        json.dump(params, f, indent=2)
    LOGGER.info("Saved hyperparameters to %s", output_path)


def load_hyperparameters(input_path: Path) -> Dict[str, float | int]:
    """Load hyperparameters from JSON file."""
    with input_path.open("r") as f:
        params = json.load(f)
    LOGGER.info("Loaded hyperparameters from %s", input_path)
    return params


def save_embeddings(
    connection: sqlite3.Connection,
    user_ids: List[str],
    release_ids: List[int],
    user_embeddings: np.ndarray,
    item_embeddings: np.ndarray,
    n_components: int,
) -> None:
    """Save embeddings to database."""
    LOGGER.info("Saving embeddings to database...")
    start = perf_counter()

    # Create tables if they don't exist
    connection.execute("""
        CREATE TABLE IF NOT EXISTS user_embeddings (
            id_user TEXT PRIMARY KEY REFERENCES users(id_user),
            embedding_json TEXT NOT NULL,
            n_factors INTEGER NOT NULL,
            last_updated TEXT NOT NULL
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS release_embeddings (
            id_release INTEGER PRIMARY KEY REFERENCES releases(id_release),
            embedding_json TEXT NOT NULL,
            n_factors INTEGER NOT NULL,
            last_updated TEXT NOT NULL
        )
    """)

    # Clear existing embeddings
    connection.execute("DELETE FROM user_embeddings;")
    connection.execute("DELETE FROM release_embeddings;")

    # Save user embeddings
    user_data = []
    for idx, user_id in enumerate(user_ids):
        embedding = user_embeddings[idx].tolist()
        user_data.append(
            (
                user_id,
                json.dumps(embedding),
                n_components,
            )
        )

    connection.executemany(
        """
        INSERT INTO user_embeddings (id_user, embedding_json, n_factors, last_updated)
        VALUES (?, ?, ?, datetime('now'))
        """,
        user_data,
    )

    # Save release embeddings
    release_data = []
    for idx, release_id in enumerate(release_ids):
        embedding = item_embeddings[idx].tolist()
        release_data.append(
            (
                release_id,
                json.dumps(embedding),
                n_components,
            )
        )

    connection.executemany(
        """
        INSERT INTO release_embeddings (id_release, embedding_json, n_factors, last_updated)
        VALUES (?, ?, ?, datetime('now'))
        """,
        release_data,
    )

    connection.commit()

    elapsed = perf_counter() - start
    LOGGER.info(
        "Saved %d user embeddings and %d release embeddings (%.2fs)",
        len(user_ids),
        len(release_ids),
        elapsed,
    )


def build_embeddings(
    connection: sqlite3.Connection,
    min_rating: float = 3.0,
    n_components: int = 30,
    max_iter: int = 200,
    random_state: int = 42,
    min_user_ratings: int = 15,
    min_release_ratings: int = 10,
    alpha_W: float = 0.001,
    alpha_H: float = 0.001,
    l1_ratio: float = 0.0,
) -> None:
    """Main function to build and save NMF embeddings.

    Args:
        alpha_W: L2 regularization for user embeddings (default: 0.001, reduced from 0.01)
        alpha_H: L2 regularization for item embeddings (default: 0.001, reduced from 0.01)
        l1_ratio: Mix of L1/L2 regularization (default: 0.0, reduced from 0.1)
    """
    import gc

    total_start = perf_counter()

    # Load matrix
    matrix, user_ids, release_ids, _, _ = load_user_item_matrix(
        connection, min_rating, min_user_ratings, min_release_ratings
    )

    # Force garbage collection to free memory before training
    gc.collect()

    # Train NMF with improved hyperparameters
    user_embeddings, item_embeddings = train_nmf(
        matrix,
        n_components=n_components,
        max_iter=max_iter,
        random_state=random_state,
        alpha_W=alpha_W,
        alpha_H=alpha_H,
        l1_ratio=l1_ratio,
    )

    # Free matrix memory before saving
    del matrix
    gc.collect()

    # Save embeddings
    save_embeddings(
        connection,
        user_ids,
        release_ids,
        user_embeddings,
        item_embeddings,
        n_components,
    )

    LOGGER.info("Total time: %.2fs", perf_counter() - total_start)


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s - %(message)s")


def parse_arguments(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=str,
        help="Path to the Sputnik SQLite database (defaults to data/sputnik.db)",
    )

    # Optimization mode
    optimization_group = parser.add_mutually_exclusive_group()
    optimization_group.add_argument(
        "--optimize",
        action="store_true",
        help="Only optimize hyperparameters (do not train final model)",
    )
    optimization_group.add_argument(
        "--optimize-and-train",
        action="store_true",
        help="Optimize hyperparameters and then train final model with best parameters",
    )
    optimization_group.add_argument(
        "--load-params",
        type=str,
        help="Load hyperparameters from JSON file",
    )

    parser.add_argument(
        "--n-calls",
        type=int,
        default=20,
        help="Number of optimization iterations (default: 20)",
    )
    parser.add_argument(
        "--save-params",
        type=str,
        nargs="?",
        const="models/NMF/nmf_params.json",
        help=(
            "Save optimized hyperparameters to JSON file "
            "(default: models/NMF/nmf_params.json if --optimize-and-train is used)"
        ),
    )
    parser.add_argument(
        "--metric",
        type=str,
        choices=["mse", "ndcg"],
        default="ndcg",
        help=(
            "Evaluation metric for optimization: 'mse' (faster) or "
            "'ndcg' (better for recommendations, default)"
        ),
    )
    parser.add_argument(
        "--ndcg-k",
        type=int,
        default=9,
        help="Number of recommendations for NDCG@k evaluation (default: 9)",
    )
    parser.add_argument(
        "--checkpoint-dir",
        type=str,
        help="Directory to save optimization checkpoints (enables checkpointing)",
    )
    parser.add_argument(
        "--resume-from",
        type=str,
        help="Path to checkpoint file to resume optimization from",
    )

    # Training parameters (used when not optimizing)
    parser.add_argument(
        "--min-rating",
        type=float,
        default=3.0,
        help="Minimum rating considered a positive interaction",
    )
    parser.add_argument(
        "--n-components",
        type=int,
        default=30,
        help="Number of latent factors (default: 30, reduced for low-memory systems)",
    )
    parser.add_argument(
        "--max-iter",
        type=int,
        default=200,
        help="Maximum iterations for NMF (default: 200)",
    )
    parser.add_argument(
        "--random-state",
        type=int,
        default=42,
        help="Random seed for reproducibility",
    )
    parser.add_argument(
        "--min-user-ratings",
        type=int,
        default=15,
        help=(
            "Minimum positive ratings per user to include "
            "(default: 15, increased for low-memory systems)"
        ),
    )
    parser.add_argument(
        "--min-release-ratings",
        type=int,
        default=10,
        help=(
            "Minimum positive ratings per release to include "
            "(default: 10, increased for low-memory systems)"
        ),
    )
    parser.add_argument(
        "--alpha-w",
        type=float,
        default=0.001,
        help="L2 regularization for user embeddings (default: 0.001, reduced from 0.01)",
    )
    parser.add_argument(
        "--alpha-h",
        type=float,
        default=0.001,
        help="L2 regularization for item embeddings (default: 0.001, reduced from 0.01)",
    )
    parser.add_argument(
        "--l1-ratio",
        type=float,
        default=0.0,
        help=(
            "Mix of L1/L2 regularization, 0.0=only L2, 1.0=only L1 (default: 0.0, reduced from 0.1)"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_arguments(argv)
    configure_logging(args.verbose)

    database_path = resolve_database_path(args.database)
    LOGGER.info("Using database: %s", database_path)

    if not database_path.exists():
        LOGGER.error("Database not found: %s", database_path)
        return 1

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.execute("PRAGMA journal_mode = WAL;")

        # Determine hyperparameters to use
        if args.load_params:
            # Load parameters from file
            params_path = Path(args.load_params).expanduser()
            # If relative path, try models/NMF/ first
            if not params_path.is_absolute():
                models_dir = Path(__file__).resolve().parents[1] / "models" / "NMF"
                potential_path = models_dir / params_path.name
                if potential_path.exists():
                    params_path = potential_path
                else:
                    params_path = params_path.resolve()
            else:
                params_path = params_path.resolve()

            if not params_path.exists():
                LOGGER.error("Parameters file not found: %s", params_path)
                return 1
            params = load_hyperparameters(params_path)
            n_components = int(params["n_components"])
            max_iter = int(params["max_iter"])
            alpha_W = float(params["alpha_W"])
            alpha_H = float(params["alpha_H"])
            l1_ratio = float(params["l1_ratio"])
            LOGGER.info("Using loaded parameters: %s", params)
        elif args.optimize or args.optimize_and_train:
            # Optimize hyperparameters
            if not SKOPT_AVAILABLE:
                LOGGER.error(
                    "scikit-optimize is required for hyperparameter optimization. "
                    "Install it with: pip install scikit-optimize"
                )
                return 1

            # Load matrix for optimization
            LOGGER.info("Loading matrix for optimization...")
            matrix, user_ids, release_ids, _, _ = load_user_item_matrix(
                connection, args.min_rating, args.min_user_ratings, args.min_release_ratings
            )

            # Prepare checkpoint directory if specified
            checkpoint_dir = None
            if args.checkpoint_dir:
                checkpoint_dir = Path(args.checkpoint_dir).expanduser().resolve()

            # Prepare resume checkpoint if specified
            resume_from = None
            if args.resume_from:
                resume_from = Path(args.resume_from).expanduser().resolve()

            # Run optimization
            params = optimize_hyperparameters(
                matrix=matrix,
                user_ids=user_ids,
                release_ids=release_ids,
                n_calls=args.n_calls,
                random_state=args.random_state,
                metric=args.metric,
                k=args.ndcg_k,
                checkpoint_dir=checkpoint_dir,
                resume_from=resume_from,
            )

            # Save parameters if requested
            if args.save_params:
                save_path = Path(args.save_params).expanduser()
                # If it's the default path (models/NMF/nmf_params.json), resolve it properly
                if save_path.parts[0] == "models" and len(save_path.parts) >= 2:
                    # It's a path like models/NMF/nmf_params.json
                    models_dir = Path(__file__).resolve().parents[1] / "models" / "NMF"
                    models_dir.mkdir(parents=True, exist_ok=True)
                    save_path = models_dir / save_path.name
                elif not save_path.is_absolute():
                    # Relative path, save in models/NMF/ directory
                    models_dir = Path(__file__).resolve().parents[1] / "models" / "NMF"
                    models_dir.mkdir(parents=True, exist_ok=True)
                    save_path = models_dir / save_path.name
                else:
                    save_path = save_path.resolve()
                    # Ensure parent directory exists
                    save_path.parent.mkdir(parents=True, exist_ok=True)
                save_hyperparameters(params, save_path)
            elif args.optimize_and_train:
                # Auto-save when using --optimize-and-train without --save-params
                models_dir = Path(__file__).resolve().parents[1] / "models" / "NMF"
                models_dir.mkdir(parents=True, exist_ok=True)
                save_path = models_dir / "nmf_params.json"
                save_hyperparameters(params, save_path)

            # Extract parameters
            n_components = int(params["n_components"])
            max_iter = int(params["max_iter"])
            alpha_W = float(params["alpha_W"])
            alpha_H = float(params["alpha_H"])
            l1_ratio = float(params["l1_ratio"])

            # If only optimizing, exit here
            if args.optimize:
                LOGGER.info(
                    "Optimization complete. Use --optimize-and-train to also train the model."
                )
                return 0

            # If optimize_and_train, continue to training below
            LOGGER.info("Training final model with optimized parameters...")
        else:
            # Use command-line arguments
            n_components = args.n_components
            max_iter = args.max_iter
            alpha_W = args.alpha_w
            alpha_H = args.alpha_h
            l1_ratio = args.l1_ratio

        # Train and save embeddings
        build_embeddings(
            connection,
            min_rating=args.min_rating,
            n_components=n_components,
            max_iter=max_iter,
            random_state=args.random_state,
            min_user_ratings=args.min_user_ratings,
            min_release_ratings=args.min_release_ratings,
            alpha_W=alpha_W,
            alpha_H=alpha_H,
            l1_ratio=l1_ratio,
        )

    LOGGER.info("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
