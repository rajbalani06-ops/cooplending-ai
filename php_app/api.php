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
$demoText = 'राजारामबापू सह. बँक लि., खाते क्र: ९८४५०१२३८९, खातेदार: विठ्ठल सखाराम कदम, पॅन: ABCDE1234F, मासिक शिल्लक: रु. ३२,०००/-';
$rawText = ($input['action'] ?? '') === 'demo' ? $demoText : trim((string)($input['raw_text'] ?? ''));
if ($rawText === '' || mb_strlen($rawText) > 20000) respond(422, ['error' => 'raw_text is required and must be at most 20,000 characters']);

$workerUrl = getenv('AI_SERVICE_URL') ?: 'http://python_service:8001/api/v1/extract';
$curl = curl_init($workerUrl);
curl_setopt_array($curl, [
    CURLOPT_POST => true, CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HTTPHEADER => ['Content-Type: application/json', 'Accept: application/json'],
    CURLOPT_POSTFIELDS => json_encode(['raw_text' => $rawText], JSON_UNESCAPED_UNICODE),
    CURLOPT_CONNECTTIMEOUT => 5, CURLOPT_TIMEOUT => 90,
]);
$responseBody = curl_exec($curl);
$curlError = curl_error($curl);
$httpCode = (int)curl_getinfo($curl, CURLINFO_HTTP_CODE);
curl_close($curl);
if ($responseBody === false) respond(502, ['error' => 'Could not reach AI extraction service', 'detail' => $curlError]);
$assessment = json_decode($responseBody, true);
if ($httpCode < 200 || $httpCode >= 300 || !is_array($assessment)) {
    respond($httpCode >= 400 && $httpCode < 600 ? $httpCode : 502, ['error' => $assessment['detail'] ?? 'AI extraction failed']);
}

try {
    $pdo = db();
    $pdo->beginTransaction();
    $insert = $pdo->prepare('INSERT INTO loan_applications
        (applicant_name_english, applicant_name_marathi, bank_or_society_name, account_number, pan_number, monthly_income, eligibility_score, decision_status, risk_flags, raw_ocr_input)
        VALUES (:english, :marathi, :bank, :account, :pan, :income, :score, :status, :flags, :raw)');
    $insert->execute([
        ':english' => $assessment['applicant_name_english'], ':marathi' => $assessment['applicant_name_marathi'],
        ':bank' => $assessment['bank_or_society_name'], ':account' => $assessment['account_number'],
        ':pan' => $assessment['pan_number'], ':income' => $assessment['monthly_income'],
        ':score' => $assessment['eligibility_score'], ':status' => $assessment['status'],
        ':flags' => json_encode($assessment['risk_flags'], JSON_UNESCAPED_UNICODE), ':raw' => $rawText,
    ]);
    $applicationId = (int)$pdo->lastInsertId();
    $borrower = $pdo->prepare('INSERT INTO borrowers (applicant_name_english, applicant_name_marathi, pan_number, district)
        VALUES (:english, :marathi, :pan, :district)
        ON DUPLICATE KEY UPDATE applicant_name_english = VALUES(applicant_name_english), applicant_name_marathi = VALUES(applicant_name_marathi), district = VALUES(district)');
    $borrower->execute([':english' => $assessment['applicant_name_english'], ':marathi' => $assessment['applicant_name_marathi'], ':pan' => $assessment['pan_number'], ':district' => $assessment['district']]);
    $audit = $pdo->prepare('INSERT INTO risk_audit_logs (application_id, eligibility_score, decision_status, risk_flags, engine_version) VALUES (?, ?, ?, ?, ?)');
    $audit->execute([$applicationId, $assessment['eligibility_score'], $assessment['status'], json_encode($assessment['risk_flags']), 'rules-v1']);
    $pdo->commit();
    $assessment['application_id'] = $applicationId;
    respond(201, $assessment);
} catch (Throwable $error) {
    if (isset($pdo) && $pdo->inTransaction()) $pdo->rollBack();
    error_log('CoopLending persistence error: ' . $error->getMessage());
    respond(500, ['error' => 'Could not save extracted application']);
}
