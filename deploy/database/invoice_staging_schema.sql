/* =============================================================================
   Document Automation: invoice staging schema (Oracle 12.1.0.2 or later)
   -----------------------------------------------------------------------------
   Run by the DBA, once, in a custom schema (for example XXDA). The application
   NEVER runs DDL; it only INSERTs posted invoices and UPDATEs their status.

   Purpose
     The app writes reviewed ("saved") invoices here. A separate step (PL/SQL or
     concurrent program) resolves supplier / org / UOM / tax mappings and copies
     the rows into the Oracle EBS R12 Payables open interface:
         XXDA_INVOICE_HEADERS  -> AP_INVOICES_INTERFACE
         XXDA_INVOICE_LINES    -> AP_INVOICE_LINES_INTERFACE
     followed by the standard "Payables Open Interface Import" program.

   Design rules
     1. Column names of the existing tables are kept (SELLER_*, BUYER_*,
        FBR_INVOICE_NO, SALE_INVOICE_NO, ...) so current reports keep working.
     2. Raw values read from the document are kept next to the resolved EBS IDs
        (SELLER_NTN next to VENDOR_ID), so a wrong mapping can always be traced.
     3. One row per invoice of a document: (DOCUMENT_ID, FIRST_PAGE) is unique,
        so pressing "Post" twice can never create two rows.
     4. Text columns use CHAR semantics so Urdu / Unicode text is not cut short
        on an AL32UTF8 database.
     5. Lengths that EBS limits more tightly (DESCRIPTION 240, UOM 25) are
        trimmed in the mapping step, not here, so nothing read from the document
        is lost. Check the exact lengths against your R12 instance.

   Objects
     XXDA_INVOICE_HEADERS   one row per invoice
     XXDA_INVOICE_LINES     item lines (and optional tax / freight lines)
     XXDA_INVOICE_STATUS_HISTORY  every status change and retry (append only)
     XXDA_INVOICE_FIELD_AUDIT     OCR value vs final value, per field
     XXDA_SUPPLIER_MAP      seller NTN / STRN  -> EBS VENDOR_ID + VENDOR_SITE_ID
     XXDA_ORG_MAP           buyer NTN          -> EBS ORG_ID (operating unit)
     XXDA_UOM_MAP           UOM text on paper  -> EBS UNIT_OF_MEASURE
   ============================================================================= */


/* -----------------------------------------------------------------------------
   1. Invoice headers
   ----------------------------------------------------------------------------- */
CREATE TABLE XXDA_INVOICE_HEADERS (
    INVOICE_ID              NUMBER GENERATED ALWAYS AS IDENTITY (START WITH 1 NOCACHE) NOT NULL,

    -- Link to the document in the app (storage/documents/<DOCUMENT_ID>)
    DOCUMENT_ID             CHAR(32 BYTE)          NOT NULL,   -- 32-hex document id
    DOCUMENT_REF            VARCHAR2(20 CHAR)      NOT NULL,   -- DOC-2026-0001
    FIRST_PAGE              NUMBER(5)              NOT NULL,   -- first page of this invoice
    LAST_PAGE               NUMBER(5)              NOT NULL,
    SOURCE_FILE             VARCHAR2(260 CHAR),
    SOURCE_SHA256           CHAR(64 BYTE),                     -- fingerprint of the uploaded file

    -- Invoice identity
    SALE_INVOICE_NO         VARCHAR2(50 CHAR),                 -- supplier's invoice no. -> AP INVOICE_NUM (50)
    FBR_INVOICE_NO          VARCHAR2(100 CHAR),                -- FBR / POS invoice no.
    QR_FBR_INVOICE_NO       VARCHAR2(100 CHAR),                -- FBR no. read from the QR code
    QR_CODE_VALUE           CLOB,
    INVOICE_DATE            DATE,
    DUE_DATE                DATE,                              -- EBS normally derives it from TERMS
    INVOICE_TYPE            VARCHAR2(25 CHAR)      DEFAULT 'STANDARD' NOT NULL,  -- STANDARD | CREDIT | DEBIT
    SALE_TYPE_CODE          VARCHAR2(10 CHAR),
    SALE_TYPE_DESC          VARCHAR2(200 CHAR),

    -- References printed on the invoice
    PO_NUMBER               VARCHAR2(50 CHAR),                 -- AP PO_NUMBER is 20; trimmed/validated on mapping
    DC_NUMBER               VARCHAR2(50 CHAR),                 -- delivery challan
    GATE_PASS_NO            VARCHAR2(50 CHAR),
    CONTRACT_NO             VARCHAR2(50 CHAR),

    -- Seller = supplier (vendor) as printed
    SELLER_NAME             VARCHAR2(240 CHAR),
    SELLER_NTN              VARCHAR2(20 CHAR),                 -- stored normalised: digits and '-' only
    SELLER_STRN             VARCHAR2(30 CHAR),
    SELLER_ADDRESS          VARCHAR2(500 CHAR),
    SELLER_PROVINCE         VARCHAR2(50 CHAR),
    SELLER_REG_TYPE         VARCHAR2(50 CHAR),

    -- Buyer = our company / operating unit as printed
    BUYER_NAME              VARCHAR2(240 CHAR),
    BUYER_NTN               VARCHAR2(20 CHAR),
    BUYER_STRN              VARCHAR2(30 CHAR),
    BUYER_ADDRESS           VARCHAR2(500 CHAR),
    BUYER_PROVINCE          VARCHAR2(50 CHAR),
    BUYER_REG_TYPE          VARCHAR2(50 CHAR),

    -- Amounts (currency of the invoice)
    CURRENCY_CODE           VARCHAR2(15 CHAR)      DEFAULT 'PKR' NOT NULL,  -- AP INVOICE_CURRENCY_CODE
    TAX_RATE                NUMBER(5,2),                       -- sales tax %, e.g. 18.00
    TOTAL_QUANTITY          NUMBER(18,3),
    TOTAL_VALUE_EXCL_TAX    NUMBER(18,2),                      -- subtotal before tax
    DISCOUNT_AMOUNT         NUMBER(18,2),
    TOTAL_SALES_TAX         NUMBER(18,2),
    TOTAL_FURTHER_TAX       NUMBER(18,2),
    GRAND_TOTAL             NUMBER(18,2),                      -- AP INVOICE_AMOUNT
    AMOUNT_IN_WORDS         VARCHAR2(500 CHAR),
    WHT_RATE                NUMBER(7,3),                       -- withholding: EBS applies it through an AWT group
    WHT_AMOUNT              NUMBER(18,2),

    -- Review / extraction
    EXTRACTION_METHOD       VARCHAR2(10 CHAR)      DEFAULT 'OCR' NOT NULL,  -- OCR | QR | MANUAL
    OCR_CONFIDENCE          NUMBER(5,4),                       -- 0..1, average of the fields
    NEEDS_REVIEW            NUMBER(1)              DEFAULT 0 NOT NULL,
    WARNINGS                VARCHAR2(4000 CHAR),
    EXTRACTED_JSON          CLOB,                              -- what OCR read, before user corrections (audit)
    REVIEWED_BY             VARCHAR2(100 CHAR),
    REVIEWED_AT             TIMESTAMP(6),

    -- Tracking: where the data came from and how much the user had to correct
    UPLOADED_BY             VARCHAR2(100 CHAR),
    UPLOADED_AT             TIMESTAMP(6),
    PAGE_COUNT              NUMBER(5),                         -- pages of the whole document
    OCR_ENGINE              VARCHAR2(30 CHAR),                 -- onnxruntime | paddle
    OCR_MODEL               VARCHAR2(100 CHAR),                -- e.g. PP-OCRv6_small_det / _rec
    OCR_DPI                 NUMBER(4),
    OCR_COMPLETED_AT        TIMESTAMP(6),
    APP_VERSION             VARCHAR2(20 CHAR),
    FIELDS_EXTRACTED        NUMBER(4),                         -- fields OCR filled
    FIELDS_CORRECTED        NUMBER(4),                         -- fields the user changed
    LINES_EXTRACTED         NUMBER(4),
    LINES_CORRECTED         NUMBER(4),                         -- lines added, removed or edited
    POST_ATTEMPTS           NUMBER(4)              DEFAULT 1 NOT NULL,
    IMPORT_ATTEMPTS         NUMBER(4)              DEFAULT 0 NOT NULL,

    -- EBS mapping (filled by the mapping step, not by OCR)
    ORG_ID                  NUMBER(15),                        -- operating unit (from XXDA_ORG_MAP)
    VENDOR_ID               NUMBER(15),                        -- AP_SUPPLIERS.VENDOR_ID
    VENDOR_NUM              VARCHAR2(30 CHAR),
    VENDOR_SITE_ID          NUMBER(15),                        -- AP_SUPPLIER_SITES_ALL.VENDOR_SITE_ID
    VENDOR_SITE_CODE        VARCHAR2(15 CHAR),
    TERMS_NAME              VARCHAR2(50 CHAR),
    GL_DATE                 DATE,
    EXCHANGE_RATE_TYPE      VARCHAR2(30 CHAR),                 -- only for non-PKR invoices
    EXCHANGE_RATE           NUMBER,
    EXCHANGE_DATE           DATE,
    AWT_GROUP_NAME          VARCHAR2(25 CHAR),                 -- withholding tax group
    PAYMENT_METHOD_CODE     VARCHAR2(30 CHAR),
    DESCRIPTION             VARCHAR2(240 CHAR),                -- AP DESCRIPTION (240)

    -- Integration status
    STATUS                  VARCHAR2(20 CHAR)      DEFAULT 'POSTED' NOT NULL,
    STATUS_MESSAGE          VARCHAR2(4000 CHAR),
    EBS_GROUP_ID            VARCHAR2(80 CHAR),                 -- AP_INVOICES_INTERFACE.GROUP_ID of the import batch
    EBS_INTERFACE_ID        NUMBER(15),                        -- AP_INVOICES_INTERFACE.INVOICE_ID
    EBS_INVOICE_ID          NUMBER(15),                        -- AP_INVOICES_ALL.INVOICE_ID after import
    EBS_REQUEST_ID          NUMBER(15),                        -- concurrent request of the import
    INTERFACED_AT           TIMESTAMP(6),
    IMPORTED_AT             TIMESTAMP(6),

    -- Audit
    POSTED_BY               VARCHAR2(100 CHAR)     NOT NULL,
    CREATED_AT              TIMESTAMP(6)           DEFAULT SYSTIMESTAMP NOT NULL,
    LAST_UPDATED_BY         VARCHAR2(100 CHAR),
    LAST_UPDATED_AT         TIMESTAMP(6),
    ROW_VERSION             NUMBER(10)             DEFAULT 1 NOT NULL,   -- optimistic locking

    CONSTRAINT XXDA_INV_HDR_PK          PRIMARY KEY (INVOICE_ID),
    CONSTRAINT XXDA_INV_HDR_DOC_UK      UNIQUE (DOCUMENT_ID, FIRST_PAGE),
    CONSTRAINT XXDA_INV_HDR_PAGES_CK    CHECK (FIRST_PAGE >= 1 AND LAST_PAGE >= FIRST_PAGE),
    CONSTRAINT XXDA_INV_HDR_REVIEW_CK   CHECK (NEEDS_REVIEW IN (0, 1)),
    CONSTRAINT XXDA_INV_HDR_METHOD_CK   CHECK (EXTRACTION_METHOD IN ('OCR', 'QR', 'MANUAL')),
    CONSTRAINT XXDA_INV_HDR_TYPE_CK     CHECK (INVOICE_TYPE IN ('STANDARD', 'CREDIT', 'DEBIT')),
    CONSTRAINT XXDA_INV_HDR_CONF_CK     CHECK (OCR_CONFIDENCE IS NULL OR OCR_CONFIDENCE BETWEEN 0 AND 1),
    CONSTRAINT XXDA_INV_HDR_JSON_CK     CHECK (EXTRACTED_JSON IS NULL OR EXTRACTED_JSON IS JSON),
    /* Status flow:
         POSTED      written by the app after "Post to database"
         VALIDATED   supplier / org / UOM / tax mappings resolved, totals checked
         INTERFACED  copied into AP_INVOICES_INTERFACE / AP_INVOICE_LINES_INTERFACE
         IMPORTED    created in AP (EBS_INVOICE_ID set)
         REJECTED    mapping or import failed (STATUS_MESSAGE says why); can be fixed and retried
         CANCELLED   withdrawn; frees the duplicate check below */
    CONSTRAINT XXDA_INV_HDR_STATUS_CK   CHECK (STATUS IN ('POSTED', 'VALIDATED', 'INTERFACED', 'IMPORTED', 'REJECTED', 'CANCELLED'))
);

/* Duplicate protection, as in EBS: one active invoice number per supplier.
   Uses the NTN, or the name when the NTN was not printed. Cancelled rows are ignored. */
CREATE UNIQUE INDEX XXDA_INV_HDR_DUP_UK ON XXDA_INVOICE_HEADERS (
    CASE WHEN STATUS <> 'CANCELLED' AND SALE_INVOICE_NO IS NOT NULL
         THEN NVL(SELLER_NTN, UPPER(SELLER_NAME)) END,
    CASE WHEN STATUS <> 'CANCELLED' AND SALE_INVOICE_NO IS NOT NULL
         THEN UPPER(SALE_INVOICE_NO) END
);

CREATE INDEX XXDA_INV_HDR_STATUS_IX ON XXDA_INVOICE_HEADERS (STATUS, CREATED_AT);
CREATE INDEX XXDA_INV_HDR_SELLER_IX ON XXDA_INVOICE_HEADERS (SELLER_NTN);
CREATE INDEX XXDA_INV_HDR_DOCREF_IX ON XXDA_INVOICE_HEADERS (DOCUMENT_REF);


/* -----------------------------------------------------------------------------
   2. Invoice lines
   ----------------------------------------------------------------------------- */
CREATE TABLE XXDA_INVOICE_LINES (
    ITEM_ID                 NUMBER GENERATED ALWAYS AS IDENTITY (START WITH 1 NOCACHE) NOT NULL,
    INVOICE_ID              NUMBER                 NOT NULL,
    LINE_NO                 NUMBER(4)              NOT NULL,
    LINE_TYPE               VARCHAR2(25 CHAR)      DEFAULT 'ITEM' NOT NULL,  -- AP LINE_TYPE_LOOKUP_CODE

    -- As printed on the invoice
    HS_CODE                 VARCHAR2(12 CHAR),
    DESCRIPTION             VARCHAR2(500 CHAR),                -- AP line DESCRIPTION is 240: trimmed on mapping
    PROCESS                 VARCHAR2(100 CHAR),
    QUANTITY                NUMBER(18,3),                      -- AP QUANTITY_INVOICED
    UOM                     VARCHAR2(30 CHAR),
    UNIT_PRICE              NUMBER(18,4),                      -- rate per unit; AP UNIT_PRICE
    VALUE_EXCL_TAX          NUMBER(18,2),                      -- AP line AMOUNT (ITEM line)
    DISCOUNT_AMOUNT         NUMBER(18,2),
    TAX_RATE                NUMBER(5,2),
    SALES_TAX               NUMBER(18,2),
    FURTHER_TAX             NUMBER(18,2),
    TOTAL_VALUE             NUMBER(18,2),                      -- value including taxes
    OCR_CONFIDENCE          NUMBER(5,4),
    LINE_SOURCE             VARCHAR2(10 CHAR)      DEFAULT 'OCR' NOT NULL,   -- OCR | EDITED | ADDED
    SOURCE_PAGE             NUMBER(5),                         -- page the line was read from
    EXTRACTED_JSON          CLOB,                              -- the line as OCR read it (audit)

    -- EBS mapping (filled by the mapping step)
    EBS_UOM                 VARCHAR2(25 CHAR),                 -- AP UNIT_OF_MEAS_LOOKUP_CODE (from XXDA_UOM_MAP)
    INVENTORY_ITEM_ID       NUMBER(15),
    PO_NUMBER               VARCHAR2(20 CHAR),
    PO_LINE_NUMBER          NUMBER,
    PO_SHIPMENT_NUM         NUMBER,
    RECEIPT_NUMBER          VARCHAR2(30 CHAR),
    TAX_CLASSIFICATION_CODE VARCHAR2(30 CHAR),                 -- E-Business Tax code for sales tax
    DIST_CODE_CONCATENATED  VARCHAR2(250 CHAR),                -- GL account, when not matched to a PO
    DIST_CODE_COMBINATION_ID NUMBER(15),
    EBS_INTERFACE_LINE_ID   NUMBER(15),                        -- AP_INVOICE_LINES_INTERFACE.INVOICE_LINE_ID

    CONSTRAINT XXDA_INV_LINE_PK         PRIMARY KEY (ITEM_ID),
    CONSTRAINT XXDA_INV_LINE_HDR_FK     FOREIGN KEY (INVOICE_ID)
                                        REFERENCES XXDA_INVOICE_HEADERS (INVOICE_ID) ON DELETE CASCADE,
    CONSTRAINT XXDA_INV_LINE_NO_UK      UNIQUE (INVOICE_ID, LINE_NO),
    CONSTRAINT XXDA_INV_LINE_NO_CK      CHECK (LINE_NO >= 1),
    CONSTRAINT XXDA_INV_LINE_TYPE_CK    CHECK (LINE_TYPE IN ('ITEM', 'TAX', 'FREIGHT', 'MISCELLANEOUS')),
    CONSTRAINT XXDA_INV_LINE_CONF_CK    CHECK (OCR_CONFIDENCE IS NULL OR OCR_CONFIDENCE BETWEEN 0 AND 1),
    CONSTRAINT XXDA_INV_LINE_SRC_CK     CHECK (LINE_SOURCE IN ('OCR', 'EDITED', 'ADDED')),
    CONSTRAINT XXDA_INV_LINE_JSON_CK    CHECK (EXTRACTED_JSON IS NULL OR EXTRACTED_JSON IS JSON)
);

CREATE INDEX XXDA_INV_LINE_HDR_IX ON XXDA_INVOICE_LINES (INVOICE_ID);


/* -----------------------------------------------------------------------------
   2a. Tracking: every status change of an invoice (append only, never updated)
       One row per step: POSTED, VALIDATED, INTERFACED, IMPORTED, REJECTED,
       CANCELLED, and every retry. Answers "who did what, when, and why it failed".
   ----------------------------------------------------------------------------- */
CREATE TABLE XXDA_INVOICE_STATUS_HISTORY (
    HISTORY_ID              NUMBER GENERATED ALWAYS AS IDENTITY (START WITH 1 NOCACHE) NOT NULL,
    INVOICE_ID              NUMBER                 NOT NULL,
    FROM_STATUS             VARCHAR2(20 CHAR),                 -- NULL for the first row
    TO_STATUS               VARCHAR2(20 CHAR)      NOT NULL,
    ACTION                  VARCHAR2(30 CHAR)      NOT NULL,   -- POST | REPOST | VALIDATE | INTERFACE | IMPORT | CANCEL | RETRY
    MESSAGE                 VARCHAR2(4000 CHAR),               -- error text or note
    EBS_GROUP_ID            VARCHAR2(80 CHAR),
    EBS_REQUEST_ID          NUMBER(15),
    CHANGED_BY              VARCHAR2(100 CHAR)     NOT NULL,   -- app user or EBS user / program
    CHANGED_FROM            VARCHAR2(100 CHAR),                -- host or program name
    CHANGED_AT              TIMESTAMP(6)           DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT XXDA_INV_HIST_PK         PRIMARY KEY (HISTORY_ID),
    CONSTRAINT XXDA_INV_HIST_HDR_FK     FOREIGN KEY (INVOICE_ID)
                                        REFERENCES XXDA_INVOICE_HEADERS (INVOICE_ID) ON DELETE CASCADE
);

CREATE INDEX XXDA_INV_HIST_HDR_IX ON XXDA_INVOICE_STATUS_HISTORY (INVOICE_ID, CHANGED_AT);


/* -----------------------------------------------------------------------------
   2b. Tracking: OCR value vs final value, per header field
       One row per field of every posted invoice. Shows how accurate OCR is per
       field and per supplier, and exactly what the reviewer changed.
   ----------------------------------------------------------------------------- */
CREATE TABLE XXDA_INVOICE_FIELD_AUDIT (
    AUDIT_ID                NUMBER GENERATED ALWAYS AS IDENTITY (START WITH 1 NOCACHE) NOT NULL,
    INVOICE_ID              NUMBER                 NOT NULL,
    FIELD_KEY               VARCHAR2(40 CHAR)      NOT NULL,   -- app key, e.g. supplier_ntn
    COLUMN_NAME             VARCHAR2(30 CHAR),                 -- staging column, e.g. SELLER_NTN
    EXTRACTED_VALUE         VARCHAR2(4000 CHAR),               -- what OCR read
    FINAL_VALUE             VARCHAR2(4000 CHAR),               -- what was posted
    WAS_CORRECTED           NUMBER(1)              DEFAULT 0 NOT NULL,
    OCR_CONFIDENCE          NUMBER(5,4),
    SOURCE_PAGE             NUMBER(5),                         -- page and OCR line the value came from
    SOURCE_LINE             NUMBER(5),
    CREATED_AT              TIMESTAMP(6)           DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT XXDA_INV_AUD_PK          PRIMARY KEY (AUDIT_ID),
    CONSTRAINT XXDA_INV_AUD_HDR_FK      FOREIGN KEY (INVOICE_ID)
                                        REFERENCES XXDA_INVOICE_HEADERS (INVOICE_ID) ON DELETE CASCADE,
    CONSTRAINT XXDA_INV_AUD_UK          UNIQUE (INVOICE_ID, FIELD_KEY),
    CONSTRAINT XXDA_INV_AUD_CORR_CK     CHECK (WAS_CORRECTED IN (0, 1))
);


/* -----------------------------------------------------------------------------
   3. Mapping tables (maintained by the finance / EBS team)
   ----------------------------------------------------------------------------- */
CREATE TABLE XXDA_SUPPLIER_MAP (
    SELLER_NTN              VARCHAR2(20 CHAR)      NOT NULL,
    SELLER_STRN             VARCHAR2(30 CHAR),
    ORG_ID                  NUMBER(15)             NOT NULL,
    VENDOR_ID               NUMBER(15)             NOT NULL,
    VENDOR_SITE_ID          NUMBER(15)             NOT NULL,
    TERMS_NAME              VARCHAR2(50 CHAR),
    AWT_GROUP_NAME          VARCHAR2(25 CHAR),
    ENABLED_FLAG            CHAR(1 CHAR)           DEFAULT 'Y' NOT NULL,
    CREATED_AT              TIMESTAMP(6)           DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT XXDA_SUP_MAP_PK          PRIMARY KEY (SELLER_NTN, ORG_ID),
    CONSTRAINT XXDA_SUP_MAP_FLAG_CK     CHECK (ENABLED_FLAG IN ('Y', 'N'))
);

CREATE TABLE XXDA_ORG_MAP (
    BUYER_NTN               VARCHAR2(20 CHAR)      NOT NULL,
    ORG_ID                  NUMBER(15)             NOT NULL,
    ORG_NAME                VARCHAR2(240 CHAR),
    ENABLED_FLAG            CHAR(1 CHAR)           DEFAULT 'Y' NOT NULL,
    CONSTRAINT XXDA_ORG_MAP_PK          PRIMARY KEY (BUYER_NTN),
    CONSTRAINT XXDA_ORG_MAP_FLAG_CK     CHECK (ENABLED_FLAG IN ('Y', 'N'))
);

CREATE TABLE XXDA_UOM_MAP (
    SOURCE_UOM              VARCHAR2(30 CHAR)      NOT NULL,   -- as printed, upper case: KG, PCS, NOS, MT
    EBS_UOM                 VARCHAR2(25 CHAR)      NOT NULL,   -- MTL_UNITS_OF_MEASURE.UNIT_OF_MEASURE
    CONSTRAINT XXDA_UOM_MAP_PK          PRIMARY KEY (SOURCE_UOM)
);


/* -----------------------------------------------------------------------------
   4. Grants for the application user (INSERT/UPDATE only, no DDL, no DELETE)
      Replace DOCAUTO_APP with the user configured in ORACLE_USER.
   ----------------------------------------------------------------------------- */
-- GRANT SELECT, INSERT, UPDATE ON XXDA_INVOICE_HEADERS TO DOCAUTO_APP;
-- GRANT SELECT, INSERT          ON XXDA_INVOICE_LINES   TO DOCAUTO_APP;
-- GRANT SELECT, INSERT          ON XXDA_INVOICE_STATUS_HISTORY TO DOCAUTO_APP;
-- GRANT SELECT, INSERT          ON XXDA_INVOICE_FIELD_AUDIT    TO DOCAUTO_APP;
-- GRANT SELECT                  ON XXDA_SUPPLIER_MAP    TO DOCAUTO_APP;
-- GRANT SELECT                  ON XXDA_ORG_MAP         TO DOCAUTO_APP;
-- GRANT SELECT                  ON XXDA_UOM_MAP         TO DOCAUTO_APP;
-- The EBS mapping program (APPS) additionally needs SELECT, UPDATE on headers and lines.
