CREATE DATABASE IF NOT EXISTS cooplending CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE cooplending;

CREATE TABLE IF NOT EXISTS loan_applications (
    id INT AUTO_INCREMENT PRIMARY KEY,
    applicant_name_english VARCHAR(150) NOT NULL,
    applicant_name_marathi VARCHAR(150) NOT NULL,
    bank_or_society_name VARCHAR(200) NOT NULL,
    account_number VARCHAR(50) NOT NULL,
    pan_number VARCHAR(10) NULL,
    monthly_income DECIMAL(12, 2) NOT NULL,
    eligibility_score INT NOT NULL,
    decision_status ENUM('AUTO_APPROVED', 'MANUAL_REVIEW', 'REJECTED') NOT NULL,
    risk_flags TEXT NOT NULL,
    raw_ocr_input TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_created_at (created_at),
    INDEX idx_decision_status (decision_status),
    CONSTRAINT chk_eligibility_score CHECK (eligibility_score BETWEEN 0 AND 100)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS borrowers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    applicant_name_english VARCHAR(150) NOT NULL,
    applicant_name_marathi VARCHAR(150) NOT NULL,
    pan_number VARCHAR(10) NULL,
    district VARCHAR(100) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_borrower_pan (pan_number)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS risk_audit_logs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    application_id INT NOT NULL,
    eligibility_score INT NOT NULL,
    decision_status ENUM('AUTO_APPROVED', 'MANUAL_REVIEW', 'REJECTED') NOT NULL,
    risk_flags JSON NOT NULL,
    engine_version VARCHAR(32) NOT NULL DEFAULT 'rules-v1',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_risk_application FOREIGN KEY (application_id) REFERENCES loan_applications(id) ON DELETE CASCADE,
    INDEX idx_risk_created_at (created_at)
) ENGINE=InnoDB;
