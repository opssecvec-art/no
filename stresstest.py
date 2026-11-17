import sys
import time
import concurrent.futures
import requests

def fire_request(url, session_id, request_id):
    headers = {"X-Test-Session": f"test-client-{session_id}"}
    start_time = time.time()
    try:
        # Hitting a heavy endpoint (e.g., search or report generation)
        response = requests.get(f"{url}?q=test&export=true", headers=headers, timeout=10)
        duration = time.time() - start_time
        return (request_id, response.status_code, duration)
    except requests.exceptions.RequestException as e:
        return (request_id, "ERROR", str(e))

def stress_endpoint(target_url, total_requests=100, concurrency=20):
    print(f"[*] Launching {total_requests} requests with concurrency {concurrency} against {target_url}...")
    
    success_counts = {}
    start_all = time.time()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(fire_request, target_url, i % 5, i) 
            for i in range(total_requests)
        ]
        
        for future in concurrent.futures.as_completed(futures):
            req_id, status, duration = future.result()
            success_counts[status] = success_counts.get(status, 0) + 1
            print(f"[Req {req_id}] Status: {status} | Latency: {duration if isinstance(duration, float) else 'N/A'}s")

    print("\n--- Summary ---")
    for status, count in success_counts.items():
        print(f"Status {status}: {count} requests")
    print(f"Total Time: {time.time() - start_all:.2f} seconds")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python stress_test.py <http://target-url/search>")
        sys.exit(1)
    stress_endpoint(sys.argv[1])
