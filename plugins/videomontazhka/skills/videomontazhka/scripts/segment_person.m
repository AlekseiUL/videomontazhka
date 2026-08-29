#import <AVFoundation/AVFoundation.h>
#import <CoreImage/CoreImage.h>
#import <Foundation/Foundation.h>
#import <ImageIO/CGImageProperties.h>
#import <Vision/Vision.h>

#include <math.h>

static NSString *const SPRUTErrorDomain = @"ai.sprut.person-matte";

typedef NS_ENUM(NSUInteger, SPRUTOutputMode) {
    SPRUTOutputModeMatte,
    SPRUTOutputModeForeground,
};

static void PrintUsage(const char *program) {
    fprintf(stderr,
            "Usage: %s <input-video> --mode matte|foreground "
            "[--quality fast|balanced|accurate]\n"
            "\n"
            "Runs Apple's local Vision person segmentation on every source frame and\n"
            "writes headerless BGRA frames to stdout. Foreground mode keeps source RGB\n"
            "and stores the matte in alpha; matte mode writes opaque grayscale. The\n"
            "safe person_matte.py wrapper supplies geometry/FPS to FFmpeg and encodes\n"
            "video-only ProRes 4444 inside an approval-gated edit directory. No source\n"
            "audio is copied and no data leaves the Mac.\n",
            program);
}

static NSError *SPRUTError(NSString *description) {
    return [NSError errorWithDomain:SPRUTErrorDomain
                               code:1
                           userInfo:@{NSLocalizedDescriptionKey: description}];
}

static CGImagePropertyOrientation OrientationForTransform(CGAffineTransform transform) {
    const double epsilon = 0.01;
    BOOL a0 = fabs(transform.a) < epsilon;
    BOOL b0 = fabs(transform.b) < epsilon;
    BOOL c0 = fabs(transform.c) < epsilon;
    BOOL d0 = fabs(transform.d) < epsilon;

    if (a0 && transform.b > 0.0 && transform.c < 0.0 && d0) {
        return kCGImagePropertyOrientationRight;
    }
    if (a0 && transform.b < 0.0 && transform.c > 0.0 && d0) {
        return kCGImagePropertyOrientationLeft;
    }
    if (transform.a < 0.0 && b0 && c0 && transform.d < 0.0) {
        return kCGImagePropertyOrientationDown;
    }
    return kCGImagePropertyOrientationUp;
}

static VNGeneratePersonSegmentationRequestQualityLevel QualityFromName(NSString *name) {
    if ([name isEqualToString:@"fast"]) {
        return VNGeneratePersonSegmentationRequestQualityLevelFast;
    }
    if ([name isEqualToString:@"balanced"]) {
        return VNGeneratePersonSegmentationRequestQualityLevelBalanced;
    }
    return VNGeneratePersonSegmentationRequestQualityLevelAccurate;
}

static BOOL CopySourceImage(CVPixelBufferRef source,
                            CVPixelBufferRef destination,
                            CGImagePropertyOrientation orientation,
                            CIContext *context,
                            size_t outputWidth,
                            size_t outputHeight,
                            NSError **error) {
    if (orientation == kCGImagePropertyOrientationUp &&
        CVPixelBufferGetPixelFormatType(source) == kCVPixelFormatType_32BGRA &&
        CVPixelBufferGetWidth(source) == outputWidth &&
        CVPixelBufferGetHeight(source) == outputHeight) {
        CVReturn sourceLock = CVPixelBufferLockBaseAddress(source, kCVPixelBufferLock_ReadOnly);
        CVReturn destinationLock = CVPixelBufferLockBaseAddress(destination, 0);
        if (sourceLock != kCVReturnSuccess || destinationLock != kCVReturnSuccess) {
            if (sourceLock == kCVReturnSuccess) {
                CVPixelBufferUnlockBaseAddress(source, kCVPixelBufferLock_ReadOnly);
            }
            if (destinationLock == kCVReturnSuccess) {
                CVPixelBufferUnlockBaseAddress(destination, 0);
            }
            if (error != NULL) {
                *error = SPRUTError(@"could not lock source/destination pixel buffers");
            }
            return NO;
        }
        const uint8_t *sourceBytes = CVPixelBufferGetBaseAddress(source);
        uint8_t *destinationBytes = CVPixelBufferGetBaseAddress(destination);
        size_t sourceStride = CVPixelBufferGetBytesPerRow(source);
        size_t destinationStride = CVPixelBufferGetBytesPerRow(destination);
        size_t rowBytes = outputWidth * 4;
        for (size_t y = 0; y < outputHeight; y++) {
            memcpy(destinationBytes + y * destinationStride,
                   sourceBytes + y * sourceStride,
                   rowBytes);
        }
        CVPixelBufferUnlockBaseAddress(destination, 0);
        CVPixelBufferUnlockBaseAddress(source, kCVPixelBufferLock_ReadOnly);
        return YES;
    }

    CIImage *image = [CIImage imageWithCVPixelBuffer:source];
    image = [image imageByApplyingOrientation:orientation];
    CGRect extent = image.extent;
    if (CGRectIsEmpty(extent) || !isfinite(extent.size.width) || !isfinite(extent.size.height)) {
        if (error != NULL) {
            *error = SPRUTError(@"decoded source image has an invalid extent");
        }
        return NO;
    }
    image = [image imageByApplyingTransform:CGAffineTransformMakeTranslation(-extent.origin.x,
                                                                               -extent.origin.y)];
    CGFloat scaleX = (CGFloat)outputWidth / extent.size.width;
    CGFloat scaleY = (CGFloat)outputHeight / extent.size.height;
    image = [image imageByApplyingTransform:CGAffineTransformMakeScale(scaleX, scaleY)];
    [context render:image
    toCVPixelBuffer:destination
             bounds:CGRectMake(0.0, 0.0, outputWidth, outputHeight)
         colorSpace:NULL];
    return YES;
}

static uint8_t BilinearMaskValue(const uint8_t *base,
                                 size_t stride,
                                 size_t maskWidth,
                                 size_t maskHeight,
                                 size_t x,
                                 size_t y,
                                 size_t outputWidth,
                                 size_t outputHeight) {
    double sourceX = ((double)x + 0.5) * (double)maskWidth / (double)outputWidth - 0.5;
    double sourceY = ((double)y + 0.5) * (double)maskHeight / (double)outputHeight - 0.5;
    sourceX = fmax(0.0, fmin((double)maskWidth - 1.0, sourceX));
    sourceY = fmax(0.0, fmin((double)maskHeight - 1.0, sourceY));
    size_t x0 = (size_t)floor(sourceX);
    size_t y0 = (size_t)floor(sourceY);
    size_t x1 = MIN(maskWidth - 1, x0 + 1);
    size_t y1 = MIN(maskHeight - 1, y0 + 1);
    double fx = sourceX - (double)x0;
    double fy = sourceY - (double)y0;
    const uint8_t *row0 = base + y0 * stride;
    const uint8_t *row1 = base + y1 * stride;
    double top = (1.0 - fx) * row0[x0] + fx * row0[x1];
    double bottom = (1.0 - fx) * row1[x0] + fx * row1[x1];
    return (uint8_t)llround((1.0 - fy) * top + fy * bottom);
}

static BOOL ApplyMask(CVPixelBufferRef _Nullable mask,
                      CVPixelBufferRef output,
                      SPRUTOutputMode mode,
                      size_t outputWidth,
                      size_t outputHeight,
                      NSError **error) {
    const uint8_t *maskBytes = NULL;
    size_t maskStride = 0;
    size_t maskWidth = 0;
    size_t maskHeight = 0;
    BOOL maskLocked = NO;
    if (mask != NULL) {
        if (CVPixelBufferGetPixelFormatType(mask) != kCVPixelFormatType_OneComponent8) {
            if (error != NULL) {
                *error = SPRUTError(@"Vision returned an unsupported mask pixel format");
            }
            return NO;
        }
        if (CVPixelBufferLockBaseAddress(mask, kCVPixelBufferLock_ReadOnly) != kCVReturnSuccess) {
            if (error != NULL) {
                *error = SPRUTError(@"could not lock Vision mask");
            }
            return NO;
        }
        maskLocked = YES;
        maskBytes = CVPixelBufferGetBaseAddress(mask);
        maskStride = CVPixelBufferGetBytesPerRow(mask);
        maskWidth = CVPixelBufferGetWidth(mask);
        maskHeight = CVPixelBufferGetHeight(mask);
        if (maskBytes == NULL || maskWidth == 0 || maskHeight == 0) {
            CVPixelBufferUnlockBaseAddress(mask, kCVPixelBufferLock_ReadOnly);
            if (error != NULL) {
                *error = SPRUTError(@"Vision returned an empty mask");
            }
            return NO;
        }
    }

    if (CVPixelBufferLockBaseAddress(output, 0) != kCVReturnSuccess) {
        if (maskLocked) {
            CVPixelBufferUnlockBaseAddress(mask, kCVPixelBufferLock_ReadOnly);
        }
        if (error != NULL) {
            *error = SPRUTError(@"could not lock output pixel buffer");
        }
        return NO;
    }
    uint8_t *outputBytes = CVPixelBufferGetBaseAddress(output);
    size_t outputStride = CVPixelBufferGetBytesPerRow(output);
    for (size_t y = 0; y < outputHeight; y++) {
        uint8_t *row = outputBytes + y * outputStride;
        for (size_t x = 0; x < outputWidth; x++) {
            uint8_t alpha = maskBytes != NULL
                ? BilinearMaskValue(maskBytes, maskStride, maskWidth, maskHeight,
                                    x, y, outputWidth, outputHeight)
                : 0;
            if (mode == SPRUTOutputModeForeground) {
                row[x * 4 + 3] = alpha;
            } else {
                row[x * 4 + 0] = alpha;
                row[x * 4 + 1] = alpha;
                row[x * 4 + 2] = alpha;
                row[x * 4 + 3] = 255;
            }
        }
    }
    CVPixelBufferUnlockBaseAddress(output, 0);
    if (maskLocked) {
        CVPixelBufferUnlockBaseAddress(mask, kCVPixelBufferLock_ReadOnly);
    }
    return YES;
}

static CVPixelBufferRef _Nullable NewOutputPixelBuffer(size_t width,
                                                        size_t height,
                                                        NSError **error) {
    NSDictionary *attributes = @{
        (NSString *)kCVPixelBufferIOSurfacePropertiesKey: @{},
        (NSString *)kCVPixelBufferCGImageCompatibilityKey: @YES,
        (NSString *)kCVPixelBufferCGBitmapContextCompatibilityKey: @YES,
    };
    CVPixelBufferRef output = NULL;
    CVReturn status = CVPixelBufferCreate(kCFAllocatorDefault,
                                          width,
                                          height,
                                          kCVPixelFormatType_32BGRA,
                                          (__bridge CFDictionaryRef)attributes,
                                          &output);
    if (status != kCVReturnSuccess || output == NULL) {
        if (error != NULL) {
            *error = SPRUTError([NSString stringWithFormat:@"could not allocate output frame (%d)",
                                                          status]);
        }
        return NULL;
    }
    return output;
}

static BOOL WriteRawFrame(CVPixelBufferRef pixelBuffer,
                          size_t width,
                          size_t height,
                          NSError **error) {
    if (CVPixelBufferLockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly) != kCVReturnSuccess) {
        if (error != NULL) {
            *error = SPRUTError(@"could not lock output frame for stdout");
        }
        return NO;
    }
    const uint8_t *base = CVPixelBufferGetBaseAddress(pixelBuffer);
    size_t stride = CVPixelBufferGetBytesPerRow(pixelBuffer);
    size_t rowBytes = width * 4;
    BOOL succeeded = YES;
    for (size_t y = 0; y < height; y++) {
        if (fwrite(base + y * stride, 1, rowBytes, stdout) != rowBytes) {
            succeeded = NO;
            break;
        }
    }
    CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    if (!succeeded && error != NULL) {
        *error = SPRUTError(@"could not write raw frame to stdout");
    }
    return succeeded;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc == 2 && (strcmp(argv[1], "--help") == 0 || strcmp(argv[1], "-h") == 0)) {
            PrintUsage(argv[0]);
            return 0;
        }
        if (argc < 4) {
            PrintUsage(argv[0]);
            return 2;
        }

        NSString *inputPath = [[NSString stringWithUTF8String:argv[1]] stringByStandardizingPath];
        NSString *qualityName = @"accurate";
        BOOL modeWasSet = NO;
        SPRUTOutputMode mode = SPRUTOutputModeForeground;
        for (int index = 2; index < argc; index++) {
            NSString *argument = [NSString stringWithUTF8String:argv[index]];
            if ([argument isEqualToString:@"--mode"] && index + 1 < argc) {
                NSString *modeName = [NSString stringWithUTF8String:argv[++index]];
                if ([modeName isEqualToString:@"matte"]) {
                    mode = SPRUTOutputModeMatte;
                } else if ([modeName isEqualToString:@"foreground"]) {
                    mode = SPRUTOutputModeForeground;
                } else {
                    fprintf(stderr, "Invalid mode: %s\n", modeName.UTF8String);
                    return 2;
                }
                modeWasSet = YES;
            } else if ([argument isEqualToString:@"--quality"] && index + 1 < argc) {
                qualityName = [NSString stringWithUTF8String:argv[++index]];
                if (![@[@"fast", @"balanced", @"accurate"] containsObject:qualityName]) {
                    fprintf(stderr, "Invalid quality: %s\n", qualityName.UTF8String);
                    return 2;
                }
            } else {
                fprintf(stderr, "Unknown or incomplete argument: %s\n", argv[index]);
                PrintUsage(argv[0]);
                return 2;
            }
        }
        if (!modeWasSet) {
            fprintf(stderr, "--mode matte|foreground is required.\n");
            return 2;
        }
        if (![[NSFileManager defaultManager] fileExistsAtPath:inputPath]) {
            fprintf(stderr, "Input video does not exist: %s\n", inputPath.UTF8String);
            return 3;
        }

        AVURLAsset *asset = [AVURLAsset URLAssetWithURL:[NSURL fileURLWithPath:inputPath]
                                                options:@{AVURLAssetPreferPreciseDurationAndTimingKey: @YES}];
        __block NSArray<AVAssetTrack *> *videoTracks = nil;
        __block NSError *trackLoadError = nil;
        dispatch_semaphore_t trackSemaphore = dispatch_semaphore_create(0);
        [asset loadTracksWithMediaType:AVMediaTypeVideo
                    completionHandler:^(NSArray<AVAssetTrack *> *tracks, NSError *error) {
                        videoTracks = tracks;
                        trackLoadError = error;
                        dispatch_semaphore_signal(trackSemaphore);
                    }];
        dispatch_semaphore_wait(trackSemaphore, DISPATCH_TIME_FOREVER);
        if (trackLoadError != nil || videoTracks.firstObject == nil) {
            fprintf(stderr, "Could not load a video track: %s\n",
                    (trackLoadError.localizedDescription ?: @"input has no video track").UTF8String);
            return 3;
        }
        AVAssetTrack *videoTrack = videoTracks.firstObject;
        CGAffineTransform preferredTransform = videoTrack.preferredTransform;
        CGRect naturalRect = CGRectMake(0.0, 0.0,
                                        videoTrack.naturalSize.width,
                                        videoTrack.naturalSize.height);
        CGRect displayRect = CGRectApplyAffineTransform(naturalRect, preferredTransform);
        size_t outputWidth = (size_t)MAX(1, (NSInteger)llround(fabs(displayRect.size.width)));
        size_t outputHeight = (size_t)MAX(1, (NSInteger)llround(fabs(displayRect.size.height)));
        CGImagePropertyOrientation orientation = OrientationForTransform(preferredTransform);

        NSError *readerError = nil;
        AVAssetReader *reader = [[AVAssetReader alloc] initWithAsset:asset error:&readerError];
        if (reader == nil) {
            fprintf(stderr, "Could not open input: %s\n", readerError.localizedDescription.UTF8String);
            return 3;
        }
        NSDictionary *readerSettings = @{
            (NSString *)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_32BGRA),
        };
        AVAssetReaderTrackOutput *readerOutput =
            [[AVAssetReaderTrackOutput alloc] initWithTrack:videoTrack outputSettings:readerSettings];
        readerOutput.alwaysCopiesSampleData = NO;
        if (![reader canAddOutput:readerOutput]) {
            fprintf(stderr, "AVAssetReader rejected BGRA decoding.\n");
            return 3;
        }
        [reader addOutput:readerOutput];
        if (![reader startReading]) {
            fprintf(stderr, "Could not start decoding: %s\n", reader.error.localizedDescription.UTF8String);
            return 3;
        }

        VNGeneratePersonSegmentationRequest *segmentationRequest =
            [[VNGeneratePersonSegmentationRequest alloc] init];
        segmentationRequest.qualityLevel = QualityFromName(qualityName);
        segmentationRequest.outputPixelFormat = kCVPixelFormatType_OneComponent8;
        VNSequenceRequestHandler *sequenceHandler = [[VNSequenceRequestHandler alloc] init];
        CIContext *ciContext = [CIContext contextWithOptions:@{
            kCIContextWorkingColorSpace: NSNull.null,
        }];

        const size_t preferredBufferSize = 4 * 1024 * 1024;
        setvbuf(stdout, NULL, _IOFBF, preferredBufferSize);
        NSUInteger frameCount = 0;
        NSError *processingError = nil;
        BOOL failed = NO;
        CMSampleBufferRef sampleBuffer = NULL;
        while ((sampleBuffer = [readerOutput copyNextSampleBuffer]) != NULL) {
            @autoreleasepool {
                NSError *visionError = nil;
                BOOL visionOK = [sequenceHandler performRequests:@[segmentationRequest]
                                                  onCMSampleBuffer:sampleBuffer
                                                       orientation:orientation
                                                             error:&visionError];
                if (!visionOK) {
                    processingError = visionError ?: SPRUTError(@"Vision segmentation failed");
                    failed = YES;
                }
                VNPixelBufferObservation *observation = visionOK
                    ? (VNPixelBufferObservation *)segmentationRequest.results.firstObject
                    : nil;
                CVPixelBufferRef maskBuffer = observation != nil ? observation.pixelBuffer : NULL;
                CVPixelBufferRef outputPixelBuffer = NULL;
                if (!failed) {
                    outputPixelBuffer = NewOutputPixelBuffer(outputWidth,
                                                             outputHeight,
                                                             &processingError);
                    failed = outputPixelBuffer == NULL;
                }
                if (!failed && mode == SPRUTOutputModeForeground) {
                    failed = !CopySourceImage(CMSampleBufferGetImageBuffer(sampleBuffer),
                                              outputPixelBuffer,
                                              orientation,
                                              ciContext,
                                              outputWidth,
                                              outputHeight,
                                              &processingError);
                }
                if (!failed) {
                    failed = !ApplyMask(maskBuffer,
                                        outputPixelBuffer,
                                        mode,
                                        outputWidth,
                                        outputHeight,
                                        &processingError);
                }
                if (!failed) {
                    failed = !WriteRawFrame(outputPixelBuffer,
                                            outputWidth,
                                            outputHeight,
                                            &processingError);
                }
                if (outputPixelBuffer != NULL) {
                    CVPixelBufferRelease(outputPixelBuffer);
                }
                if (!failed) {
                    frameCount++;
                }
            }
            CFRelease(sampleBuffer);
            if (failed) {
                break;
            }
        }
        if (!failed && reader.status == AVAssetReaderStatusFailed) {
            processingError = reader.error ?: SPRUTError(@"video decoding failed");
            failed = YES;
        }
        if (!failed && frameCount == 0) {
            processingError = SPRUTError(@"input produced zero video frames");
            failed = YES;
        }
        if (fflush(stdout) != 0 && !failed) {
            processingError = SPRUTError(@"could not flush raw output stream");
            failed = YES;
        }
        if (failed) {
            [reader cancelReading];
            fprintf(stderr, "person segmentation failed: %s\n",
                    (processingError.localizedDescription ?: @"unknown error").UTF8String);
            return 5;
        }
        fprintf(stderr,
                "Processed %lu raw BGRA frames at %zux%zu, mode=%s, quality=%s, audio=omitted.\n",
                (unsigned long)frameCount,
                outputWidth,
                outputHeight,
                mode == SPRUTOutputModeForeground ? "foreground" : "matte",
                qualityName.UTF8String);
        return 0;
    }
}
