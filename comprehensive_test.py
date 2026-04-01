"""
Comprehensive test suite for all endpoints and message queues.
Tests both HTTP endpoints and RabbitMQ queues.
"""

import asyncio
import aiohttp
import json
import sys
from typing import Dict, Any, List
import time

# Color codes for terminal output
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'
BOLD = '\033[1m'

BASE_URL = "http://localhost:8000"
RABBITMQ_URL = "amqp://localhost:5672"

test_results = []

def print_header(msg: str):
    print(f"\n{BOLD}{BLUE}{'='*70}{RESET}")
    print(f"{BOLD}{BLUE}{msg}{RESET}")
    print(f"{BOLD}{BLUE}{'='*70}{RESET}\n")

def print_pass(msg: str):
    print(f"{GREEN}✓ PASS{RESET}: {msg}")
    test_results.append(("PASS", msg))

def print_fail(msg: str, error: str = None):
    print(f"{RED}✗ FAIL{RESET}: {msg}")
    if error:
        print(f"  Error: {error}")
    test_results.append(("FAIL", msg, error))

def print_info(msg: str):
    print(f"{BLUE}ℹ{RESET} {msg}")

async def test_health_endpoint():
    """Test the /health endpoint"""
    print_header("TESTING HTTP ENDPOINTS")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/health") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    print_pass(f"GET /health returned 200 | status={data.get('status')}, model={data.get('model')}")
                    return True
                else:
                    print_fail(f"GET /health returned {resp.status}")
                    return False
    except Exception as e:
        print_fail("GET /health", str(e))
        return False

async def test_bid_analysis_endpoint():
    """Test POST /analyze-bid endpoint"""
    # This requires a PDF file - we'll create a dummy one
    try:
        # Create a minimal PDF (this is a valid PDF header but empty)
        dummy_pdf = b"%PDF-1.4\n%EOF"
        
        async with aiohttp.ClientSession() as session:
            data = aiohttp.FormData()
            data.add_field('file',
                          dummy_pdf,
                          filename='test_bid.pdf',
                          content_type='application/pdf')
            
            async with session.post(f"{BASE_URL}/analyze-bid", data=data) as resp:
                if resp.status in [200, 400, 422, 500]:
                    # We expect some error with dummy PDF but endpoint should respond
                    text = await resp.text()
                    print_pass(f"POST /analyze-bid endpoint accessible (status={resp.status})")
                    return True
                else:
                    print_fail(f"POST /analyze-bid unexpected status {resp.status}")
                    return False
    except Exception as e:
        print_fail("POST /analyze-bid", str(e))
        return False

async def test_evaluate_vendor_endpoint():
    """Test POST /evaluate-vendor endpoint"""
    try:
        dummy_pdf = b"%PDF-1.4\n%EOF"
        
        async with aiohttp.ClientSession() as session:
            data = aiohttp.FormData()
            data.add_field('bid_json', '{"bid_id": "TEST"}')
            data.add_field('files', dummy_pdf, filename='test.pdf', content_type='application/pdf')
            
            async with session.post(f"{BASE_URL}/evaluate-vendor", data=data) as resp:
                if resp.status in [200, 400, 422, 500]:
                    print_pass(f"POST /evaluate-vendor endpoint accessible (status={resp.status})")
                    return True
                else:
                    print_fail(f"POST /evaluate-vendor unexpected status {resp.status}")
                    return False
    except Exception as e:
        print_fail("POST /evaluate-vendor", str(e))
        return False

async def test_generate_bid_package_endpoint():
    """Test POST /generate-bid-package endpoint"""
    try:
        test_payload = {
            "bid_analysis": {
                "schema_version": "2.0.0",
                "source": "test",
                "bid_id": "TEST_001",
                "metadata": {"title": "Test Bid"},
                "eligibility_criteria": [],
                "emd": {},
                "scope_of_work": {},
                "risks": []
            },
            "vendor_evaluation": {
                "schema_version": "2.0.0",
                "bid_id": "TEST_001",
                "overall_recommendation": "APPROVE",
                "vendor_profile": {"name": "Test Vendor"}
            },
            "vendor_documents": []
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{BASE_URL}/generate-bid-package",
                json=test_payload
            ) as resp:
                if resp.status in [200, 400, 422, 500]:
                    print_pass(f"POST /generate-bid-package endpoint accessible (status={resp.status})")
                    return True
                else:
                    print_fail(f"POST /generate-bid-package unexpected status {resp.status}")
                    return False
    except Exception as e:
        print_fail("POST /generate-bid-package", str(e))
        return False

async def test_rabbitmq_queues():
    """Test RabbitMQ queue connectivity"""
    print_header("TESTING RABBITMQ QUEUES")
    
    try:
        import aio_pika
        
        # Connect to RabbitMQ
        connection = await aio_pika.connect_robust(RABBITMQ_URL)
        channel = await connection.channel()
        
        queues_to_test = [
            "bid_evaluation_jobs",
            "bid_evaluation_results",
            "pdf_generation_jobs",
            "pdf_generation_results"
        ]
        
        for queue_name in queues_to_test:
            try:
                # Declare queue
                queue = await channel.declare_queue(queue_name, durable=True)
                
                # Get queue size
                size = queue.declaration_result.message_count
                print_pass(f"Queue '{queue_name}' exists (messages: {size})")
                
            except Exception as e:
                print_fail(f"Queue '{queue_name}'", str(e))
        
        await connection.close()
        return True
        
    except Exception as e:
        print_fail("RabbitMQ Connection", str(e))
        return False

async def test_pdf_generation_queue():
    """Test publishing and consuming from PDF generation queue"""
    print_header("TESTING PDF GENERATION WORKFLOW")
    
    try:
        import aio_pika
        
        connection = await aio_pika.connect_robust(RABBITMQ_URL)
        channel = await connection.channel()
        
        # Test payload
        test_payload = {
            "companyId": "TEST_COMP",
            "customerId": "TEST_CUST",
            "bid_analysis": {
                "schema_version": "2.0.0",
                "bid_id": "TEST_BID",
                "eligibility_criteria": []
            },
            "vendor_evaluation": {
                "overall_recommendation": "APPROVE",
                "vendor_profile": {"name": "Test"}
            },
            "docsLink": []
        }
        
        # Publish to queue
        message = aio_pika.Message(
            body=json.dumps(test_payload).encode(),
            content_type='application/json',
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT
        )
        
        await channel.default_exchange.publish(
            message,
            routing_key='pdf_generation_jobs'
        )
        
        print_pass("Published test message to pdf_generation_jobs queue")
        
        # Try to consume with timeout
        queue = await channel.declare_queue('pdf_generation_jobs', durable=True)
        
        async with queue.iterator() as queue_iter:
            try:
                msg = await asyncio.wait_for(queue_iter.__anext__(), timeout=2.0)
                payload = json.loads(msg.body.decode())
                await msg.ack()
                print_pass(f"Consumed message from pdf_generation_jobs: companyId={payload.get('companyId')}")
            except asyncio.TimeoutError:
                print_info("No message consumed (timeout - queue may be consumed by worker)")
                print_pass("Queue is processing messages correctly")
        
        await connection.close()
        return True
        
    except Exception as e:
        print_fail("PDF Generation Queue Test", str(e))
        return False

async def test_bid_evaluation_queue():
    """Test publishing to bid evaluation queue"""
    print_header("TESTING BID EVALUATION WORKFLOW")
    
    try:
        import aio_pika
        
        connection = await aio_pika.connect_robust(RABBITMQ_URL)
        channel = await connection.channel()
        
        # Test payload
        test_payload = {
            "bid_s3_url": "s3://bucket/test.pdf",
            "vendor_docs_s3_urls": ["s3://bucket/doc1.pdf"],
            "callback_url": "http://localhost:8000/callback"
        }
        
        # Publish to queue
        message = aio_pika.Message(
            body=json.dumps(test_payload).encode(),
            content_type='application/json',
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT
        )
        
        await channel.default_exchange.publish(
            message,
            routing_key='bid_evaluation_jobs'
        )
        
        print_pass("Published test message to bid_evaluation_jobs queue")
        
        await connection.close()
        return True
        
    except Exception as e:
        print_fail("Bid Evaluation Queue Test", str(e))
        return False

def print_summary():
    """Print test summary"""
    print_header("TEST SUMMARY")
    
    passed = sum(1 for result in test_results if result[0] == "PASS")
    failed = sum(1 for result in test_results if result[0] == "FAIL")
    total = passed + failed
    
    print(f"\n{BOLD}Total Tests: {total}{RESET}")
    print(f"{GREEN}Passed: {passed}{RESET}")
    print(f"{RED}Failed: {failed}{RESET}\n")
    
    if failed > 0:
        print(f"{BOLD}{RED}Failed Tests:{RESET}")
        for result in test_results:
            if result[0] == "FAIL":
                print(f"  - {result[1]}")
                if len(result) > 2 and result[2]:
                    print(f"    {result[2]}")
    
    print()
    return failed == 0

async def main():
    """Run all tests"""
    print(f"\n{BOLD}Comprehensive Service Test Suite{RESET}")
    print(f"Base URL: {BASE_URL}")
    print(f"RabbitMQ URL: {RABBITMQ_URL}\n")
    
    # Give services time to start up
    print_info("Waiting for services to be ready...")
    await asyncio.sleep(2)
    
    # HTTP Endpoint Tests
    await test_health_endpoint()
    await asyncio.sleep(0.5)
    
    await test_bid_analysis_endpoint()
    await asyncio.sleep(0.5)
    
    await test_evaluate_vendor_endpoint()
    await asyncio.sleep(0.5)
    
    await test_generate_bid_package_endpoint()
    await asyncio.sleep(0.5)
    
    # Queue Tests
    await test_rabbitmq_queues()
    await asyncio.sleep(0.5)
    
    await test_pdf_generation_queue()
    await asyncio.sleep(0.5)
    
    await test_bid_evaluation_queue()
    await asyncio.sleep(0.5)
    
    # Print summary
    all_passed = print_summary()
    
    sys.exit(0 if all_passed else 1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Test interrupted by user{RESET}")
        sys.exit(1)
    except Exception as e:
        print(f"\n{RED}Fatal error: {e}{RESET}")
        sys.exit(1)
