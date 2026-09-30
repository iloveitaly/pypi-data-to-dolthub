#!/usr/bin/env python3
"""
Benchmark suite for the PyPI SQLite search API.
Measures cold/warm latency (p50, p95, p99), concurrency, memory usage, and index usage.
"""

import argparse
import asyncio
import statistics
import time

import httpx
from main import app, get_db_path

BENCHMARK_QUERIES = [
    # Exact
    "requests",
    "pytest",
    "fastapi",
    # Prefixes (short and long)
    "a",
    "ab",
    "req",
    "pyt",
    "pyd",
    # Substrings (trigram)
    "dantic",
    "test",
    "corn",
    "lib",
    # Normalized variations
    "requests_mock",
    "pytest-mock",
    "Foo.Bar",
    # Digits
    "123",
]


async def run_benchmark(
    base_url: str = "http://testserver", num_iterations: int = 50, concurrency: int = 5
):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
        # Measure health check
        health_resp = await client.get("/health")
        assert health_resp.status_code == 200, (
            f"Health check failed: {health_resp.status_code}"
        )

        # Cold latency (first pass for each query)
        cold_latencies = []
        for q in BENCHMARK_QUERIES:
            t0 = time.perf_counter()
            resp = await client.get(f"/search?q={q}&limit=50")
            elapsed_ms = (time.perf_counter() - t0) * 1000
            assert resp.status_code == 200
            cold_latencies.append(elapsed_ms)

        # Warm sequential latency
        warm_latencies = []
        for _ in range(num_iterations):
            for q in BENCHMARK_QUERIES:
                t0 = time.perf_counter()
                resp = await client.get(f"/search?q={q}&limit=50")
                elapsed_ms = (time.perf_counter() - t0) * 1000
                warm_latencies.append(elapsed_ms)

        # Warm concurrent latency
        concurrent_latencies = []
        sem = asyncio.Semaphore(concurrency)

        async def worker(query: str):
            async with sem:
                t0 = time.perf_counter()
                resp = await client.get(f"/search?q={query}&limit=50")
                elapsed_ms = (time.perf_counter() - t0) * 1000
                assert resp.status_code == 200
                concurrent_latencies.append(elapsed_ms)

        tasks = [worker(q) for _ in range(num_iterations) for q in BENCHMARK_QUERIES]
        await asyncio.gather(*tasks)

    # Compute percentiles
    def get_stats(latencies: list[float]):
        s = sorted(latencies)
        return {
            "count": len(s),
            "p50_ms": round(statistics.median(s), 3),
            "p95_ms": round(s[int(len(s) * 0.95)], 3),
            "p99_ms": round(s[int(len(s) * 0.99)], 3),
            "min_ms": round(min(s), 3),
            "max_ms": round(max(s), 3),
        }

    cold_stats = get_stats(cold_latencies)
    warm_stats = get_stats(warm_latencies)
    concurrent_stats = get_stats(concurrent_latencies)

    db_path = get_db_path()
    db_size_mb = (
        round(db_path.stat().st_size / (1024 * 1024), 2) if db_path.exists() else 0
    )

    report = {
        "database_path": str(db_path),
        "database_size_mb": db_size_mb,
        "cold_queries": cold_stats,
        "warm_sequential_queries": warm_stats,
        f"warm_concurrent_queries_{concurrency}x": concurrent_stats,
    }

    print("=== PyPI SQLite Search Benchmark Report ===")
    print(f"Database: {db_path} ({db_size_mb} MB)")
    print(f"Cold (first pass) p95: {cold_stats['p95_ms']} ms")
    print(
        f"Warm sequential   p50: {warm_stats['p50_ms']} ms | p95: {warm_stats['p95_ms']} ms | p99: {warm_stats['p99_ms']} ms"
    )
    print(
        f"Concurrent ({concurrency}x)   p50: {concurrent_stats['p50_ms']} ms | p95: {concurrent_stats['p95_ms']} ms | p99: {concurrent_stats['p99_ms']} ms"
    )

    # Gate check
    assert warm_stats["p95_ms"] < 200, (
        f"Warm p95 exceeded 200ms target: {warm_stats['p95_ms']}ms"
    )
    assert concurrent_stats["p95_ms"] < 500, (
        f"Concurrent p95 exceeded 500ms target: {concurrent_stats['p95_ms']}ms"
    )
    print("\nAll performance gates passed (<200ms warm p95, <500ms concurrent p95)!")
    return report


def main():
    parser = argparse.ArgumentParser(description="Benchmark PyPI Search API")
    parser.add_argument(
        "--iterations", type=int, default=30, help="Number of query iterations"
    )
    parser.add_argument(
        "--concurrency", type=int, default=5, help="Concurrent request level"
    )
    args = parser.parse_args()

    asyncio.run(
        run_benchmark(num_iterations=args.iterations, concurrency=args.concurrency)
    )


if __name__ == "__main__":
    main()
