<?php
declare(strict_types=1);
require_once __DIR__ . '/db.php';
header('Content-Type: application/json; charset=utf-8');
header('X-Content-Type-Options: nosniff');

function respond(int $status, array $body): never
{
    http_response_code($status);
    echo json_encode($body, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') respond(405, ['error' => 'POST required']);
$input = json_decode(file_get_contents('php://input') ?: '', true);
if (!is_array($input)) respond(400, ['error' => 'Request body must be valid JSON']);

$demoText = "राजारामबापू सह. बँक लि., खाते क्र: ९८४५०१२३८९, खातेदार: विठ्ठल सखाराम कदम, पॅन: ABCDE1234F, मासिक उत्पन्न: ४५,०००";
$rawText = ($input['action'] ?? '') === 'demo' ? $demoText : trim((string)($input['raw_text'] ?? ''));
if ($rawText === '') respond(422, ['error' => 'Document text is required.']);
$textLength = mb_strlen($rawText, 'UTF-8');
if ($textLength < 8) respond(422, ['error' => 'Document text is too short. Minimum 8 characters required.']);
if ($textLength > 20000) respond(422, ['error' => 'Document text exceeds maximum 20,000 characters limit.']);

$workerUrl = getenv('AI_SERVICE_URL') ?: 'http://python_service:8001/api/v1/extract';
$curl = curl_init($workerUrl);
curl_setopt_array($curl, [
    CURLOPT_POST => true, 
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HTTPHEADER => ['Content-Type: application/json', 'Accept: application/json'],
    CURLOPT_POSTFIELDS => json_encode(['raw_text' => $rawText], JSON_UNESCAPED_UNICODE),
    CURLOPT_CONNECTTIMEOUT => 10,
    CURLOPT_TIMEOUT => 90,
]);
$responseBody = curl_exec($curl);
$curlError = curl_error($curl);
$httpCode = (int)curl_getinfo($curl, CURLINFO_HTTP_CODE);
curl_close($curl);

if ($responseBody === false) respond(502, ['error' => 'Could not reach AI extraction service', 'detail' => $curlError]);
$assessment = json_decode($responseBody, true);

if ($httpCode < 200 || $httpCode >= 300 || !is_array($assessment)) {
    $errorMessage = 'AI extraction failed';

    if (isset($assessment['detail'])) {
        if (is_array($assessment['detail'])) {
            $messages = [];
            foreach ($assessment['detail'] as $item) {
                if (is_array($item) && isset($item['msg'])) {
                    $messages[] = preg_replace('/^Value error,\s*/i', '', (string)$item['msg']);
                }
            }
            $errorMessage = $messages ? implode('; ', $messages) : 'Invalid input format';
        } elseif (is_string($assessment['detail'])) {
            $errorMessage = $assessment['detail'];
        }
    }

    $statusCode = ($httpCode >= 400 && $httpCode < 600) ? $httpCode : 502;
    respond($statusCode, ['error' => $errorMessage]);
}

try {
    $pdo = db();
    $pdo->beginTransaction();

    $riskFlags = is_array($assessment['risk_flags'] ?? null) ? $assessment['risk_flags'] : [];
    $riskFlagsJson = json_encode($riskFlags, JSON_UNESCAPED_UNICODE);

    $insert = $pdo->prepare('INSERT INTO loan_applications 
        (applicant_name_english, applicant_name_marathi, bank_or_society_name, account_number, pan_number, monthly_income, eligibility_score, decision_status, risk_flags, raw_ocr_input)
        VALUES (:english, :marathi, :bank, :account, :pan, :income, :score, :status, :flags, :raw)');
    
    $insert->execute([
        ':english' => (string)($assessment['applicant_name_english'] ?? 'Unknown'),
        ':marathi' => (string)($assessment['applicant_name_marathi'] ?? 'अज्ञात'),
        ':bank' => (string)($assessment['bank_or_society_name'] ?? 'सहकारी संस्था'),
        ':account' => (string)($assessment['account_number'] ?? '000000'),
        ':pan' => !empty($assessment['pan_number']) ? (string)$assessment['pan_number'] : null,
        ':income' => (float)($assessment['monthly_income'] ?? 0.0),
        ':score' => (int)($assessment['eligibility_score'] ?? 0),
        ':status' => (string)($assessment['status'] ?? 'MANUAL_REVIEW'),
        ':flags' => $riskFlagsJson,
        ':raw' => $rawText,
    ]);
    
    $applicationId = (int)$pdo->lastInsertId();

    // Only update borrowers table if PAN is present
    if (!empty($assessment['pan_number'])) {
        $borrower = $pdo->prepare('INSERT INTO borrowers (applicant_name_english, applicant_name_marathi, pan_number, district)
            VALUES (:english, :marathi, :pan, :district)
            ON DUPLICATE KEY UPDATE applicant_name_english = VALUES(applicant_name_english), applicant_name_marathi = VALUES(applicant_name_marathi), district = VALUES(district)');
        $borrower->execute([
            ':english' => (string)$assessment['applicant_name_english'],
            ':marathi' => (string)$assessment['applicant_name_marathi'],
            ':pan' => (string)$assessment['pan_number'],
            ':district' => (string)($assessment['district'] ?? 'Maharashtra')
        ]);
    }

    $audit = $pdo->prepare('INSERT INTO risk_audit_logs (application_id, eligibility_score, decision_status, risk_flags, engine_version) VALUES (?, ?, ?, ?, ?)');
    $audit->execute([
        $applicationId, 
        (int)$assessment['eligibility_score'], 
        (string)$assessment['status'], 
        $riskFlagsJson, 
        'rules-v1'
    ]);

    $pdo->commit();
    $assessment['application_id'] = $applicationId;
    respond(201, $assessment);
} catch (Throwable $error) {
    if (isset($pdo) && $pdo->inTransaction()) $pdo->rollBack();
    error_log('CoopLending persistence error: ' . $error->getMessage());
    respond(500, ['error' => 'Database error: ' . $error->getMessage()]);
}
