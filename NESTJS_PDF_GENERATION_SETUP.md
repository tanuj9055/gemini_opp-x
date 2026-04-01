# NestJS Integration Setup — PDF Generation Service

## Overview

This guide walks you through integrating your NestJS server with the Python AI service for **Phase 2: Asynchronous PDF Document Generation**.

After the user approves a vendor bid, your NestJS server sends vendor documents to the Python worker, which:
1. Downloads vendor PDFs from S3
2. Generates missing compliance documents via Vertex AI Gemini
3. Merges everything into a single PDF
4. Uploads to S3 and returns the URL

---

## Prerequisites

- **NestJS** v10+ (or Express.js with amqplib)
- **Node.js** v18+
- **RabbitMQ** server running (Docker or local)
- **AWS S3 access** (for vendor document URLs)
- **Python service** running with workers active (`app.worker.main`)

---

## Step 1: Install Dependencies

```bash
npm install amqplib dotenv
npm install --save-dev @types/amqplib
```

For TypeScript projects:
```bash
npm install amqplib dotenv
npm install --save-dev @types/amqplib
```

---

## Step 2: Environment Configuration

Create `.env` file in your NestJS project root:

```env
# RabbitMQ
RABBITMQ_URL=amqp://localhost:5672

# AWS S3 (where vendor documents are stored)
AWS_S3_BUCKET=tender-demo-storage-123
AWS_REGION=us-east-1

# Service identifiers
SERVICE_NAME=nestjs-bid-system
```

---

## Step 3: Create PDF Generation Service

Create `src/services/pdf-generation.service.ts`:

```typescript
import { Injectable, OnModuleInit } from '@nestjs/common';
import * as amqp from 'amqplib';
import { Logger } from '@nestjs/common';

@Injectable()
export class PdfGenerationService implements OnModuleInit {
  private connection: amqp.Connection;
  private channel: amqp.Channel;
  private readonly logger = new Logger(PdfGenerationService.name);

  // Queue names (must match Python service)
  private readonly PDF_JOBS_QUEUE = 'pdf_generation_jobs';
  private readonly PDF_RESULTS_QUEUE = 'pdf_generation_results';

  async onModuleInit() {
    try {
      const rabbitmqUrl = process.env.RABBITMQ_URL || 'amqp://localhost:5672';
      this.connection = await amqp.connect(rabbitmqUrl);
      this.channel = await this.connection.createChannel();

      // Declare queues (durable = survives broker restart)
      await this.channel.assertQueue(this.PDF_JOBS_QUEUE, { durable: true });
      await this.channel.assertQueue(this.PDF_RESULTS_QUEUE, { durable: true });

      this.logger.log('✅ Connected to RabbitMQ');

      // Start consuming results
      this.consumePdfResults();
    } catch (error) {
      this.logger.error('Failed to connect to RabbitMQ:', error);
      throw error;
    }
  }

  /**
   * Publish PDF generation job to Python worker
   * Call this when user clicks "Generate PDF" button
   */
  async publishPdfGenerationJob(
    companyId: string,
    customerId: string,
    bidAnalysis: any,
    vendorEvaluation: any,
    vendorDocumentUrls: string[],
  ): Promise<void> {
    try {
      const payload = {
        companyId,
        customerId,
        bid_analysis: bidAnalysis,
        vendor_evaluation: vendorEvaluation,
        docsLink: vendorDocumentUrls,
      };

      const message = JSON.stringify(payload);
      
      await this.channel.sendToQueue(
        this.PDF_JOBS_QUEUE,
        Buffer.from(message),
        {
          persistent: true,
          contentType: 'application/json',
        },
      );

      this.logger.log(
        `📤 Published PDF job | Company: ${companyId} | Customer: ${customerId}`,
      );
    } catch (error) {
      this.logger.error('Failed to publish PDF job:', error);
      throw error;
    }
  }

  /**
   * Listen for PDF generation results from Python worker
   * Stores URLs in database, notifies user, etc.
   */
  private async consumePdfResults(): Promise<void> {
    try {
      await this.channel.consume(this.PDF_RESULTS_QUEUE, async (msg) => {
        if (msg) {
          const result = JSON.parse(msg.content.toString());

          this.logger.log(
            `📥 PDF Result | Company: ${result.companyId} | Status: ${result.status}`,
          );

          if (result.status === 'success') {
            // ✅ Success: PDF ready for download
            this.logger.log(`✅ PDF ready: ${result.pdf_url}`);
            
            // TODO: Store in database
            // TODO: Notify frontend/user
            // TODO: Send email with download link
            await this.handlePdfGenerationSuccess(result);
          } else {
            // ❌ Failed: Log error
            this.logger.error(`❌ PDF generation failed: ${result.error}`);
            
            // TODO: Notify user of failure
            // TODO: Log for debugging
            await this.handlePdfGenerationFailure(result);
          }

          // Acknowledge message (remove from queue)
          this.channel.ack(msg);
        }
      });

      this.logger.log('👂 Listening for PDF generation results...');
    } catch (error) {
      this.logger.error('Error consuming PDF results:', error);
    }
  }

  /**
   * Handle successful PDF generation
   * Override with your business logic
   */
  private async handlePdfGenerationSuccess(result: any): Promise<void> {
    // Example: Store in database
    const pdfRecord = {
      companyId: result.companyId,
      customerId: result.customerId,
      s3Url: result.pdf_url,
      status: 'completed',
      generatedAt: new Date(),
    };

    this.logger.debug('Storing PDF record:', pdfRecord);
    // await this.db.pdfGenerations.create(pdfRecord);
    // await this.notificationService.sendPdfReady(result.customerId, result.pdf_url);
  }

  /**
   * Handle failed PDF generation
   * Override with your business logic
   */
  private async handlePdfGenerationFailure(result: any): Promise<void> {
    const errorRecord = {
      companyId: result.companyId,
      customerId: result.customerId,
      error: result.error,
      status: 'failed',
      failedAt: new Date(),
    };

    this.logger.error('PDF generation failed:', errorRecord);
    // await this.db.pdfGenerations.create(errorRecord);
    // await this.notificationService.sendPdfError(result.customerId, result.error);
  }

  async onModuleDestroy() {
    if (this.channel) await this.channel.close();
    if (this.connection) await this.connection.close();
    this.logger.log('RabbitMQ connection closed');
  }
}
```

---

## Step 4: Create PDF Generation Controller

Create `src/controllers/pdf-generation.controller.ts`:

```typescript
import {
  Controller,
  Post,
  Body,
  BadRequestException,
  HttpCode,
  HttpStatus,
  Logger,
} from '@nestjs/common';
import { PdfGenerationService } from '../services/pdf-generation.service';

interface GeneratePdfRequest {
  companyId: string;
  customerId: string;
  bidAnalysis: any;              // Full BidAnalysisResponse from Phase 1
  vendorEvaluation: any;         // Full VendorEvaluationResponse
  vendorDocumentUrls: string[];  // S3 URLs to vendor PDFs
}

@Controller('pdf')
export class PdfGenerationController {
  private readonly logger = new Logger(PdfGenerationController.name);

  constructor(private readonly pdfService: PdfGenerationService) {}

  /**
   * POST /pdf/generate
   * Trigger PDF generation for an approved vendor
   * 
   * Request body:
   * {
   *   "companyId": "COMP_001",
   *   "customerId": "CUST_789",
   *   "bidAnalysis": { ... },
   *   "vendorEvaluation": { "overall_recommendation": "APPROVE", ... },
   *   "vendorDocumentUrls": [
   *     "s3://bucket/pan_card.pdf",
   *     "s3://bucket/gst_certificate.pdf"
   *   ]
   * }
   */
  @Post('generate')
  @HttpCode(HttpStatus.ACCEPTED)
  async generatePdf(@Body() request: GeneratePdfRequest) {
    try {
      // Validate required fields
      if (!request.companyId || !request.customerId) {
        throw new BadRequestException('companyId and customerId are required');
      }

      if (!request.bidAnalysis) {
        throw new BadRequestException('bidAnalysis is required');
      }

      if (!request.vendorEvaluation) {
        throw new BadRequestException('vendorEvaluation is required');
      }

      // Critical: Only allow if vendor is approved
      if (request.vendorEvaluation.overall_recommendation !== 'APPROVE') {
        throw new BadRequestException(
          'Vendor must be APPROVED before PDF generation',
        );
      }

      if (!Array.isArray(request.vendorDocumentUrls)) {
        throw new BadRequestException('vendorDocumentUrls must be an array');
      }

      // Publish job to Python worker
      await this.pdfService.publishPdfGenerationJob(
        request.companyId,
        request.customerId,
        request.bidAnalysis,
        request.vendorEvaluation,
        request.vendorDocumentUrls,
      );

      this.logger.log(`PDF generation queued | Company: ${request.companyId}`);

      return {
        status: 'queued',
        message: 'PDF generation job submitted. Results will be available via callback.',
        companyId: request.companyId,
        customerId: request.customerId,
      };
    } catch (error) {
      this.logger.error('PDF generation request failed:', error);
      throw error;
    }
  }

  /**
   * POST /pdf/webhook
   * Alternative: Receive PDF results via HTTP webhook instead of polling queue
   * (Optional - use if you prefer push notifications over pull)
   */
  @Post('webhook')
  @HttpCode(HttpStatus.OK)
  async receivePdfWebhook(@Body() result: any) {
    this.logger.log(`PDF webhook received | Company: ${result.companyId}`);
    
    if (result.status === 'success') {
      this.logger.log(`PDF ready: ${result.pdf_url}`);
      // Process success
    } else {
      this.logger.error(`PDF failed: ${result.error}`);
      // Process failure
    }

    return { received: true };
  }
}
```

---

## Step 5: Register Service & Controller in Module

Create or update `src/pdf-generation/pdf-generation.module.ts`:

```typescript
import { Module } from '@nestjs/common';
import { PdfGenerationService } from '../services/pdf-generation.service';
import { PdfGenerationController } from '../controllers/pdf-generation.controller';

@Module({
  providers: [PdfGenerationService],
  controllers: [PdfGenerationController],
  exports: [PdfGenerationService],
})
export class PdfGenerationModule {}
```

Add to `app.module.ts`:

```typescript
import { Module } from '@nestjs/common';
import { PdfGenerationModule } from './pdf-generation/pdf-generation.module';

@Module({
  imports: [
    // ... other modules
    PdfGenerationModule,
  ],
})
export class AppModule {}
```

---

## Step 6: Usage Example

In your bid approval flow, once user approves a vendor:

```typescript
// In your bid service or controller
import { PdfGenerationService } from './services/pdf-generation.service';

@Injectable()
export class BidService {
  constructor(private pdfService: PdfGenerationService) {}

  async approveBidAndGeneratePdf(
    companyId: string,
    customerId: string,
    bidAnalysis: any,
    vendorEvaluation: any,
  ) {
    // Step 1: Update vendor status to APPROVED
    vendorEvaluation.overall_recommendation = 'APPROVE';
    // await this.db.vendors.update(...);

    // Step 2: Get vendor document URLs from S3
    const vendorDocUrls = [
      `s3://tender-demo-storage-123/vendors/${companyId}/pan_card.pdf`,
      `s3://tender-demo-storage-123/vendors/${companyId}/gst_certificate.pdf`,
      `s3://tender-demo-storage-123/vendors/${companyId}/registration.pdf`,
    ];

    // Step 3: Trigger PDF generation
    await this.pdfService.publishPdfGenerationJob(
      companyId,
      customerId,
      bidAnalysis,
      vendorEvaluation,
      vendorDocUrls,
    );

    return {
      status: 'processing',
      message: 'PDF is being generated. You will be notified when ready.',
    };
  }
}
```

---

## Step 7: Payload Structure Reference

### Request to Python Service

```json
{
  "companyId": "COMP_001",
  "customerId": "CUST_789",
  "bid_analysis": {
    "schema_version": "2.0.0",
    "source": "gemini-2.5-pro",
    "bid_id": "GEM/2025/B/123456",
    "metadata": { "title": "Rotary Microtome" },
    "eligibility_criteria": [ ... ],
    "emd": { ... },
    "scope_of_work": { ... },
    "risks": [ ... ]
  },
  "vendor_evaluation": {
    "schema_version": "2.0.0",
    "bid_id": "GEM/2025/B/123456",
    "vendor_profile": {
      "name": "Acme Medical",
      "pan": "AABCU1234X",
      "gst": "18AABCS1234X1Z7"
    },
    "eligibility_score": 92,
    "overall_recommendation": "APPROVE",
    "acceptance_reasons": [ "Meets all criteria" ]
  },
  "docsLink": [
    "s3://bucket/pan_card.pdf",
    "s3://bucket/gst_certificate.pdf"
  ]
}
```

### Response from Python Service

**Success:**
```json
{
  "companyId": "COMP_001",
  "customerId": "CUST_789",
  "status": "success",
  "pdf_url": "s3://tender-demo-storage-123/generated_pdfs/COMP_001/a1b2c3d4.pdf",
  "error": null
}
```

**Failure:**
```json
{
  "companyId": "COMP_001",
  "customerId": "CUST_789",
  "status": "failed",
  "pdf_url": null,
  "error": "Failed to download vendor document: s3://bucket/missing.pdf"
}
```

---

## Step 8: Testing

### Manual Test with cURL

```bash
# Test PDF generation endpoint
curl -X POST http://localhost:3000/pdf/generate \
  -H "Content-Type: application/json" \
  -d '{
    "companyId": "TEST_COMP",
    "customerId": "TEST_CUST",
    "bidAnalysis": {
      "schema_version": "2.0.0",
      "bid_id": "TEST_BID",
      "eligibility_criteria": []
    },
    "vendorEvaluation": {
      "overall_recommendation": "APPROVE",
      "vendor_profile": { "name": "Test Vendor" }
    },
    "vendorDocumentUrls": [
      "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
    ]
  }'
```

Expected response:
```json
{
  "status": "queued",
  "message": "PDF generation job submitted...",
  "companyId": "TEST_COMP",
  "customerId": "TEST_CUST"
}
```

---

## Troubleshooting

### RabbitMQ Connection Fails
```
Error: Failed to connect to RabbitMQ
```
- Ensure RabbitMQ is running: `docker ps | grep rabbitmq`
- Check `RABBITMQ_URL` in `.env` matches actual RabbitMQ host/port
- Default: `amqp://localhost:5672`

### PDF Results Not Received
- Check RabbitMQ Management UI: `http://localhost:15672` (guest/guest)
- Verify `pdf_generation_results` queue exists
- Check Python worker logs for errors

### "Vendor not eligible" Error
- Ensure `vendor_evaluation.overall_recommendation === "APPROVE"`
- Check Python logs for detailed gap analysis

### S3 Upload Failures
- Verify vendor document URLs are publicly accessible or pre-signed
- Check Python `AWS_S3_BUCKET` matches your S3 bucket
- Verify AWS credentials are configured on Python server

### Long Processing Time
- PDF generation typically takes 30–60 seconds per document
- Check Vertex AI API quota: may be rate-limited
- Monitor Python worker logs for Gemini API errors

---

## Production Deployment

### RabbitMQ Setup
```bash
# Docker deployment
docker run -d \
  --name rabbitmq \
  -p 5672:5672 \
  -p 15672:15672 \
  -e RABBITMQ_DEFAULT_USER=admin \
  -e RABBITMQ_DEFAULT_PASS=secure_password \
  rabbitmq:3-management
```

### Environment Variables
```env
RABBITMQ_URL=amqp://admin:secure_password@rabbitmq-host:5672
AWS_S3_BUCKET=your-production-bucket
```

### Health Check
```typescript
// src/health/pdf-generation.health.ts
import { Injectable } from '@nestjs/common';
import { HealthIndicator, HealthCheckResult, HealthStatus } from '@nestjs/terminus';
import { PdfGenerationService } from '../services/pdf-generation.service';

@Injectable()
export class PdfGenerationHealthIndicator extends HealthIndicator {
  constructor(private pdfService: PdfGenerationService) {
    super();
  }

  async check(): Promise<HealthCheckResult> {
    try {
      // Try to access RabbitMQ
      return this.getStatus('pdf-generation', true);
    } catch {
      return this.getStatus('pdf-generation', false);
    }
  }
}
```

---

## Support & Documentation

- Full API spec: [NESTJS_INTEGRATION_GUIDE.md](./NESTJS_INTEGRATION_GUIDE.md)
- Python service: See `app/worker/pdf_consumer.py`
- RabbitMQ docs: https://www.rabbitmq.com/documentation.html

---

**Ready to generate PDFs?** 🚀
Contact the backend team with any questions about the Python service configuration.
