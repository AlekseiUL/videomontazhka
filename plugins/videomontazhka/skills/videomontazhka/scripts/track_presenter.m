#import <AVFoundation/AVFoundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <Foundation/Foundation.h>
#import <ImageIO/CGImageProperties.h>
#import <Vision/Vision.h>

#include <float.h>
#include <math.h>

static const double kDefaultAnalysisFPS = 6.0;
static const double kFaceCorrectionIntervalSeconds = 0.75;
static const float kMinimumTrackingConfidence = 0.20f;

static void PrintUsage(const char *program) {
    fprintf(stderr,
            "Usage: %s <input-video> <output.json> [--roi x,y,w,h] "
            "[--max-seconds N] [--analysis-fps N]\n"
            "\n"
            "Tracks a presenter locally with Apple Vision. Coordinates are normalized\n"
            "to [0,1] with a TOP-LEFT origin. --roi is an optional initial presenter\n"
            "rectangle in that coordinate space. The output contains rectangles only;\n"
            "it never chooses a circle, crop style, or final layout. No data is uploaded.\n",
            program);
}

static double Clamp01(double value) {
    return fmax(0.0, fmin(1.0, value));
}

static BOOL ParsePositiveDouble(NSString *text, double *value) {
    NSScanner *scanner = [NSScanner scannerWithString:text];
    double parsed = 0.0;
    if (![scanner scanDouble:&parsed] || !scanner.isAtEnd || !isfinite(parsed) || parsed <= 0.0) {
        return NO;
    }
    *value = parsed;
    return YES;
}

static BOOL ParseTopLeftROI(NSString *text, CGRect *roi) {
    NSArray<NSString *> *parts = [text componentsSeparatedByString:@","];
    if (parts.count != 4) {
        return NO;
    }

    double values[4];
    NSCharacterSet *whitespace = NSCharacterSet.whitespaceAndNewlineCharacterSet;
    for (NSUInteger index = 0; index < 4; index++) {
        NSString *part = [parts[index] stringByTrimmingCharactersInSet:whitespace];
        NSScanner *scanner = [NSScanner scannerWithString:part];
        if (![scanner scanDouble:&values[index]] || !scanner.isAtEnd || !isfinite(values[index])) {
            return NO;
        }
    }

    const double epsilon = 1e-9;
    if (values[0] < 0.0 || values[1] < 0.0 || values[2] <= 0.0 || values[3] <= 0.0 ||
        values[0] + values[2] > 1.0 + epsilon || values[1] + values[3] > 1.0 + epsilon) {
        return NO;
    }

    *roi = CGRectMake(Clamp01(values[0]), Clamp01(values[1]),
                      fmin(values[2], 1.0 - values[0]),
                      fmin(values[3], 1.0 - values[1]));
    return YES;
}

static CGRect VisionRectFromTopLeftRect(CGRect topLeft) {
    return CGRectMake(topLeft.origin.x,
                      1.0 - CGRectGetMaxY(topLeft),
                      topLeft.size.width,
                      topLeft.size.height);
}

static CGRect TopLeftRectFromVisionRect(CGRect vision) {
    double x = Clamp01(vision.origin.x);
    double y = Clamp01(1.0 - CGRectGetMaxY(vision));
    double width = fmax(0.0, fmin(vision.size.width, 1.0 - x));
    double height = fmax(0.0, fmin(vision.size.height, 1.0 - y));
    return CGRectMake(x, y, width, height);
}

static NSDictionary *ROIDIctionary(CGRect visionRect) {
    CGRect rect = TopLeftRectFromVisionRect(visionRect);
    return @{
        @"x": @(rect.origin.x),
        @"y": @(rect.origin.y),
        @"width": @(rect.size.width),
        @"height": @(rect.size.height),
    };
}

static double IntersectionOverUnion(CGRect a, CGRect b) {
    CGRect intersection = CGRectIntersection(a, b);
    if (CGRectIsNull(intersection) || CGRectIsEmpty(intersection)) {
        return 0.0;
    }
    double intersectionArea = intersection.size.width * intersection.size.height;
    double unionArea = a.size.width * a.size.height + b.size.width * b.size.height - intersectionArea;
    return unionArea > 0.0 ? intersectionArea / unionArea : 0.0;
}

static VNFaceObservation *SelectFace(NSArray<VNFaceObservation *> *faces,
                                     CGRect reference,
                                     BOOL hasReference) {
    VNFaceObservation *best = nil;
    double bestScore = -DBL_MAX;
    for (VNFaceObservation *face in faces) {
        CGRect box = face.boundingBox;
        double area = box.size.width * box.size.height;
        double score = area;
        if (hasReference) {
            double dx = CGRectGetMidX(box) - CGRectGetMidX(reference);
            double dy = CGRectGetMidY(box) - CGRectGetMidY(reference);
            double distance = hypot(dx, dy);
            score = 4.0 * IntersectionOverUnion(box, reference) - distance + 0.05 * area;
        }
        if (score > bestScore) {
            bestScore = score;
            best = face;
        }
    }
    return best;
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

static NSDictionary *FrameRecord(double relativeTime,
                                 double sourceTime,
                                 NSDictionary *roi,
                                 float confidence,
                                 NSString *status) {
    return @{
        @"time_seconds": @(relativeTime),
        @"source_time_seconds": @(sourceTime),
        @"roi": roi ?: NSNull.null,
        @"confidence": @(confidence),
        @"status": status,
    };
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc == 2 && (strcmp(argv[1], "--help") == 0 || strcmp(argv[1], "-h") == 0)) {
            PrintUsage(argv[0]);
            return 0;
        }
        if (argc < 3) {
            PrintUsage(argv[0]);
            return 2;
        }

        NSString *inputPath = [[NSString stringWithUTF8String:argv[1]] stringByStandardizingPath];
        NSString *outputPath = [[NSString stringWithUTF8String:argv[2]] stringByStandardizingPath];
        CGRect initialTopLeftROI = CGRectNull;
        BOOL hasInitialROI = NO;
        double maxSeconds = INFINITY;
        double analysisFPS = kDefaultAnalysisFPS;

        for (int index = 3; index < argc; index++) {
            NSString *argument = [NSString stringWithUTF8String:argv[index]];
            if ([argument isEqualToString:@"--roi"] && index + 1 < argc) {
                hasInitialROI = ParseTopLeftROI([NSString stringWithUTF8String:argv[++index]],
                                                &initialTopLeftROI);
                if (!hasInitialROI) {
                    fprintf(stderr, "Invalid --roi. Expected normalized top-left x,y,w,h inside [0,1].\n");
                    return 2;
                }
            } else if ([argument isEqualToString:@"--max-seconds"] && index + 1 < argc) {
                if (!ParsePositiveDouble([NSString stringWithUTF8String:argv[++index]], &maxSeconds)) {
                    fprintf(stderr, "Invalid --max-seconds. Expected a positive number.\n");
                    return 2;
                }
            } else if ([argument isEqualToString:@"--analysis-fps"] && index + 1 < argc) {
                if (!ParsePositiveDouble([NSString stringWithUTF8String:argv[++index]], &analysisFPS) ||
                    analysisFPS > 60.0) {
                    fprintf(stderr, "Invalid --analysis-fps. Expected a number in (0, 60].\n");
                    return 2;
                }
            } else {
                fprintf(stderr, "Unknown or incomplete argument: %s\n", argv[index]);
                PrintUsage(argv[0]);
                return 2;
            }
        }

        if (![[NSFileManager defaultManager] fileExistsAtPath:inputPath]) {
            fprintf(stderr, "Input video does not exist: %s\n", inputPath.UTF8String);
            return 3;
        }

        NSURL *inputURL = [NSURL fileURLWithPath:inputPath];
        AVURLAsset *asset = [AVURLAsset URLAssetWithURL:inputURL
                                                options:@{AVURLAssetPreferPreciseDurationAndTimingKey: @YES}];
        __block NSArray<AVAssetTrack *> *videoTracks = nil;
        __block NSError *trackLoadError = nil;
        dispatch_semaphore_t trackLoadSemaphore = dispatch_semaphore_create(0);
        [asset loadTracksWithMediaType:AVMediaTypeVideo
                    completionHandler:^(NSArray<AVAssetTrack *> *tracks, NSError *error) {
                        videoTracks = tracks;
                        trackLoadError = error;
                        dispatch_semaphore_signal(trackLoadSemaphore);
                    }];
        dispatch_semaphore_wait(trackLoadSemaphore, DISPATCH_TIME_FOREVER);
        if (trackLoadError != nil) {
            fprintf(stderr, "Could not inspect video tracks: %s\n",
                    trackLoadError.localizedDescription.UTF8String);
            return 3;
        }
        AVAssetTrack *videoTrack = videoTracks.firstObject;
        if (videoTrack == nil) {
            fprintf(stderr, "Input has no video track.\n");
            return 3;
        }

        NSError *readerError = nil;
        AVAssetReader *reader = [[AVAssetReader alloc] initWithAsset:asset error:&readerError];
        if (reader == nil) {
            fprintf(stderr, "Could not open video: %s\n", readerError.localizedDescription.UTF8String);
            return 3;
        }

        NSDictionary *pixelSettings = @{
            (NSString *)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_32BGRA),
        };
        AVAssetReaderTrackOutput *trackOutput =
            [[AVAssetReaderTrackOutput alloc] initWithTrack:videoTrack outputSettings:pixelSettings];
        trackOutput.alwaysCopiesSampleData = NO;
        if (![reader canAddOutput:trackOutput]) {
            fprintf(stderr, "AVAssetReader cannot decode the selected video track.\n");
            return 3;
        }
        [reader addOutput:trackOutput];
        if (![reader startReading]) {
            fprintf(stderr, "Could not start decoding: %s\n", reader.error.localizedDescription.UTF8String);
            return 3;
        }

        CGAffineTransform preferredTransform = videoTrack.preferredTransform;
        CGRect transformedBounds = CGRectApplyAffineTransform(
            CGRectMake(0.0, 0.0, videoTrack.naturalSize.width, videoTrack.naturalSize.height),
            preferredTransform);
        double displayWidth = fabs(transformedBounds.size.width);
        double displayHeight = fabs(transformedBounds.size.height);
        CGImagePropertyOrientation orientation = OrientationForTransform(preferredTransform);

        VNSequenceRequestHandler *sequenceHandler = [[VNSequenceRequestHandler alloc] init];
        VNDetectedObjectObservation *currentObservation = nil;
        CGRect lastKnownVisionBox = CGRectNull;
        BOOL hasLastKnownVisionBox = NO;
        BOOL initialROIEmitted = NO;
        double lastFaceDetectionTime = -INFINITY;

        if (hasInitialROI) {
            lastKnownVisionBox = VisionRectFromTopLeftRect(initialTopLeftROI);
            hasLastKnownVisionBox = YES;
            currentObservation = [VNDetectedObjectObservation observationWithBoundingBox:lastKnownVisionBox];
        }

        NSMutableArray<NSDictionary *> *frames = [NSMutableArray array];
        double firstSourceTime = NAN;
        double lastAnalyzedTime = -INFINITY;
        const double analysisInterval = 1.0 / analysisFPS;
        NSUInteger visibleCount = 0;
        NSUInteger missingCount = 0;

        CMSampleBufferRef sampleBuffer = NULL;
        while ((sampleBuffer = [trackOutput copyNextSampleBuffer]) != NULL) {
            double sourceTime = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer));
            if (!isfinite(sourceTime)) {
                CFRelease(sampleBuffer);
                continue;
            }
            if (!isfinite(firstSourceTime)) {
                firstSourceTime = sourceTime;
            }
            double relativeTime = fmax(0.0, sourceTime - firstSourceTime);
            if (relativeTime > maxSeconds + 1e-9) {
                CFRelease(sampleBuffer);
                break;
            }
            if (isfinite(lastAnalyzedTime) &&
                relativeTime - lastAnalyzedTime < analysisInterval - 1e-6) {
                CFRelease(sampleBuffer);
                continue;
            }
            lastAnalyzedTime = relativeTime;

            NSDictionary *roi = nil;
            float confidence = 0.0f;
            NSString *status = @"missing";

            if (hasInitialROI && !initialROIEmitted) {
                roi = ROIDIctionary(lastKnownVisionBox);
                confidence = 1.0f;
                status = @"initial_roi";
                initialROIEmitted = YES;
            } else if (currentObservation != nil) {
                VNTrackObjectRequest *request =
                    [[VNTrackObjectRequest alloc] initWithDetectedObjectObservation:currentObservation];
                request.trackingLevel = VNRequestTrackingLevelAccurate;
                NSError *visionError = nil;
                BOOL succeeded = [sequenceHandler performRequests:@[request]
                                                   onCMSampleBuffer:sampleBuffer
                                                        orientation:orientation
                                                              error:&visionError];
                VNDetectedObjectObservation *result =
                    succeeded ? (VNDetectedObjectObservation *)request.results.firstObject : nil;
                if (result != nil && result.confidence >= kMinimumTrackingConfidence &&
                    result.boundingBox.size.width > 0.0 && result.boundingBox.size.height > 0.0) {
                    currentObservation = result;
                    lastKnownVisionBox = result.boundingBox;
                    hasLastKnownVisionBox = YES;
                    roi = ROIDIctionary(result.boundingBox);
                    confidence = result.confidence;
                    status = @"tracked";
                } else {
                    currentObservation = hasLastKnownVisionBox
                        ? [VNDetectedObjectObservation observationWithBoundingBox:lastKnownVisionBox]
                        : nil;
                }
            }

            BOOL shouldDetectFace = !hasInitialROI &&
                (roi == nil || !isfinite(lastFaceDetectionTime) ||
                 relativeTime - lastFaceDetectionTime >= kFaceCorrectionIntervalSeconds);
            if (shouldDetectFace) {
                BOOL wasMissing = roi == nil;
                VNDetectFaceRectanglesRequest *faceRequest = [[VNDetectFaceRectanglesRequest alloc] init];
                VNImageRequestHandler *imageHandler =
                    [[VNImageRequestHandler alloc] initWithCMSampleBuffer:sampleBuffer
                                                              orientation:orientation
                                                                  options:@{}];
                NSError *faceError = nil;
                BOOL detected = [imageHandler performRequests:@[faceRequest] error:&faceError];
                lastFaceDetectionTime = relativeTime;
                VNFaceObservation *face = detected
                    ? SelectFace((NSArray<VNFaceObservation *> *)faceRequest.results,
                                 lastKnownVisionBox,
                                 hasLastKnownVisionBox)
                    : nil;
                if (face != nil) {
                    lastKnownVisionBox = face.boundingBox;
                    hasLastKnownVisionBox = YES;
                    currentObservation =
                        [VNDetectedObjectObservation observationWithBoundingBox:lastKnownVisionBox];
                    sequenceHandler = [[VNSequenceRequestHandler alloc] init];
                    roi = ROIDIctionary(lastKnownVisionBox);
                    confidence = face.confidence;
                    status = wasMissing ? @"face_detected" : @"face_corrected";
                }
            }

            if (roi != nil) {
                visibleCount++;
            } else {
                missingCount++;
            }
            [frames addObject:FrameRecord(relativeTime, sourceTime, roi, confidence, status)];
            CFRelease(sampleBuffer);
        }

        if (reader.status == AVAssetReaderStatusFailed) {
            fprintf(stderr, "Video decoding failed: %s\n", reader.error.localizedDescription.UTF8String);
            return 3;
        }

        double assetDuration = CMTimeGetSeconds(asset.duration);
        if (!isfinite(assetDuration)) {
            assetDuration = 0.0;
        }
        NSDictionary *requestedROI = hasInitialROI ? @{
            @"x": @(initialTopLeftROI.origin.x),
            @"y": @(initialTopLeftROI.origin.y),
            @"width": @(initialTopLeftROI.size.width),
            @"height": @(initialTopLeftROI.size.height),
        } : (NSDictionary *)NSNull.null;

        NSDictionary *document = @{
            @"version": @1,
            @"type": @"presenter_tracking_raw",
            @"coordinate_space": @"normalized_top_left",
            @"source": inputPath,
            @"video": @{
                @"display_width": @(displayWidth),
                @"display_height": @(displayHeight),
                @"duration_seconds": @(assetDuration),
                @"nominal_fps": @(videoTrack.nominalFrameRate),
            },
            @"analysis": @{
                @"engine": @"apple_vision_local",
                @"analysis_fps": @(analysisFPS),
                @"max_seconds": isfinite(maxSeconds) ? @(maxSeconds) : NSNull.null,
                @"requested_roi": requestedROI,
                @"uploads_data": @NO,
                @"geometry_decision": @"not_set",
            },
            @"summary": @{
                @"analyzed_frames": @(frames.count),
                @"frames_with_roi": @(visibleCount),
                @"missing_frames": @(missingCount),
                @"processed_seconds": @(lastAnalyzedTime > 0.0 ? lastAnalyzedTime : 0.0),
            },
            @"frames": frames,
        };

        NSError *jsonError = nil;
        NSData *json = [NSJSONSerialization dataWithJSONObject:document
                                                       options:(NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys)
                                                         error:&jsonError];
        if (json == nil) {
            fprintf(stderr, "Could not encode JSON: %s\n", jsonError.localizedDescription.UTF8String);
            return 4;
        }

        NSString *parentDirectory = [outputPath stringByDeletingLastPathComponent];
        NSError *directoryError = nil;
        if (![[NSFileManager defaultManager] createDirectoryAtPath:parentDirectory
                                       withIntermediateDirectories:YES
                                                        attributes:nil
                                                             error:&directoryError]) {
            fprintf(stderr, "Could not create output directory: %s\n",
                    directoryError.localizedDescription.UTF8String);
            return 4;
        }
        NSError *writeError = nil;
        if (![json writeToFile:outputPath options:NSDataWritingAtomic error:&writeError]) {
            fprintf(stderr, "Could not write JSON: %s\n", writeError.localizedDescription.UTF8String);
            return 4;
        }

        fprintf(stdout,
                "Wrote %lu frames (%lu with ROI, %lu missing) to %s\n",
                (unsigned long)frames.count,
                (unsigned long)visibleCount,
                (unsigned long)missingCount,
                outputPath.UTF8String);
        return 0;
    }
}
