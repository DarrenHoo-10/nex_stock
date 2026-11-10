# -*- coding: utf-8 -*-
"""Pre-multi-user (before ``2026-11-09-multi-user-p1-user-id``) SQLite DDL of the migrated tables.

Dumped from ``sqlite_master`` of a database created by the previous release; used to build
legacy databases for the multi-user migration tests.
"""

LEGACY_DDL = """
CREATE TABLE plan_account (
	id INTEGER NOT NULL,
	cash FLOAT NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE plan_holding (
	id INTEGER NOT NULL,
	code VARCHAR(10) NOT NULL,
	shares INTEGER NOT NULL,
	cost FLOAT NOT NULL,
	buy_date DATE,
	horizon VARCHAR(8),
	position INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (code)
);

CREATE TABLE plan_run (
	id INTEGER NOT NULL,
	run_id VARCHAR(64) NOT NULL,
	trade_date DATE NOT NULL,
	status VARCHAR(16) NOT NULL,
	"trigger" VARCHAR(16) NOT NULL,
	started_at DATETIME NOT NULL,
	finished_at DATETIME,
	steps_json TEXT,
	stock_total INTEGER NOT NULL,
	stock_succeeded INTEGER NOT NULL,
	stock_failed INTEGER NOT NULL,
	failures_json TEXT,
	push_json TEXT,
	push_preview TEXT,
	model_calls_json TEXT,
	data_gaps_json TEXT,
	error TEXT,
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_plan_run_run_id ON plan_run (run_id);

CREATE INDEX ix_plan_run_trade_date ON plan_run (trade_date);

CREATE TABLE plan_stock (
	id INTEGER NOT NULL,
	run_id VARCHAR(64),
	trade_date DATE NOT NULL,
	code VARCHAR(10) NOT NULL,
	status VARCHAR(16) NOT NULL,
	fingerprint VARCHAR(255),
	payload_json TEXT NOT NULL,
	meta_json TEXT,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_plan_stock_date_code ON plan_stock (trade_date, code);

CREATE INDEX ix_plan_stock_run_id ON plan_stock (run_id);

CREATE TABLE portfolio_accounts (
	id INTEGER NOT NULL,
	owner_id VARCHAR(64),
	name VARCHAR(64) NOT NULL,
	broker VARCHAR(64),
	market VARCHAR(8) NOT NULL,
	base_currency VARCHAR(8) NOT NULL,
	is_active BOOLEAN NOT NULL,
	created_at DATETIME,
	updated_at DATETIME,
	PRIMARY KEY (id)
);

CREATE INDEX ix_portfolio_account_owner_active ON portfolio_accounts (owner_id, is_active);

CREATE INDEX ix_portfolio_accounts_created_at ON portfolio_accounts (created_at);

CREATE INDEX ix_portfolio_accounts_is_active ON portfolio_accounts (is_active);

CREATE INDEX ix_portfolio_accounts_market ON portfolio_accounts (market);

CREATE INDEX ix_portfolio_accounts_owner_id ON portfolio_accounts (owner_id);

CREATE TABLE board_follow (
	id INTEGER NOT NULL,
	board_ref VARCHAR(64) NOT NULL,
	board_name VARCHAR(64) NOT NULL,
	board_type VARCHAR(16) NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (board_ref)
);

CREATE TABLE alert_rules (
	id INTEGER NOT NULL,
	name VARCHAR(64) NOT NULL,
	target_scope VARCHAR(32) NOT NULL,
	target VARCHAR(64) NOT NULL,
	alert_type VARCHAR(32) NOT NULL,
	parameters TEXT NOT NULL,
	severity VARCHAR(16) NOT NULL,
	enabled BOOLEAN NOT NULL,
	source VARCHAR(16) NOT NULL,
	cooldown_policy TEXT,
	notification_policy TEXT,
	created_at DATETIME,
	updated_at DATETIME,
	PRIMARY KEY (id)
);

CREATE INDEX ix_alert_rule_type_target ON alert_rules (alert_type, target);

CREATE INDEX ix_alert_rules_alert_type ON alert_rules (alert_type);

CREATE INDEX ix_alert_rules_created_at ON alert_rules (created_at);

CREATE INDEX ix_alert_rules_enabled ON alert_rules (enabled);

CREATE INDEX ix_alert_rules_severity ON alert_rules (severity);

CREATE INDEX ix_alert_rules_source ON alert_rules (source);

CREATE INDEX ix_alert_rules_target ON alert_rules (target);

CREATE INDEX ix_alert_rules_target_scope ON alert_rules (target_scope);

CREATE INDEX ix_alert_rules_updated_at ON alert_rules (updated_at);

CREATE TABLE conversation_session_states (
	session_id VARCHAR(100) NOT NULL,
	selected_skill_ids_json TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	active_stock_code VARCHAR(32),
	active_stock_canonical_id VARCHAR(32),
	active_stock_asset_type VARCHAR(16),
	active_stock_name VARCHAR(255),
	active_stock_source_message_id INTEGER,
	active_stock_updated_at DATETIME,
	state_version INTEGER DEFAULT 0 NOT NULL,
	session_generation VARCHAR(36),
	last_accepted_user_message_id INTEGER,
	PRIMARY KEY (session_id)
);

CREATE TABLE assistant_note (
	id INTEGER NOT NULL,
	scope VARCHAR(16) NOT NULL,
	subject VARCHAR(32),
	title VARCHAR(64) NOT NULL,
	body_md TEXT NOT NULL,
	version INTEGER NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uix_assistant_note_scope_subject UNIQUE (scope, subject)
);

CREATE UNIQUE INDEX uix_assistant_note_global ON assistant_note (scope) WHERE subject IS NULL;

CREATE TABLE llm_usage (
	id INTEGER NOT NULL,
	call_type VARCHAR(32) NOT NULL,
	model VARCHAR(128) NOT NULL,
	stock_code VARCHAR(16),
	provider VARCHAR(64),
	prompt_tokens INTEGER NOT NULL,
	completion_tokens INTEGER NOT NULL,
	total_tokens INTEGER NOT NULL,
	provider_usage_json TEXT,
	provider_usage_schema_name VARCHAR(64),
	provider_usage_schema_version VARCHAR(32),
	provider_usage_observed_at VARCHAR(32),
	normalized_prompt_tokens INTEGER,
	normalized_completion_tokens INTEGER,
	normalized_total_tokens INTEGER,
	normalized_cache_read_tokens INTEGER,
	normalized_cache_write_tokens INTEGER,
	normalized_cache_miss_tokens INTEGER,
	normalized_uncached_input_tokens INTEGER,
	normalized_cache_eligible_input_tokens INTEGER,
	normalized_cache_hit_ratio FLOAT,
	normalized_cache_write_ratio FLOAT,
	cache_capability VARCHAR(32),
	cache_eligibility VARCHAR(32),
	cache_observation VARCHAR(32),
	estimated_prefix_tokens INTEGER,
	provider_reported_prompt_tokens INTEGER,
	provider_reported_cached_tokens INTEGER,
	provider_min_cache_tokens INTEGER,
	eligibility_confidence VARCHAR(32),
	tokenizer_name VARCHAR(128),
	tokenizer_version VARCHAR(64),
	messages_hmac VARCHAR(64),
	system_message_hmac VARCHAR(64),
	user_message_hmac VARCHAR(64),
	hmac_key_version VARCHAR(64),
	hmac_domain VARCHAR(32),
	hash_scope VARCHAR(32),
	language VARCHAR(16),
	market_group VARCHAR(16),
	analysis_mode VARCHAR(64),
	legacy_prompt_mode VARCHAR(32),
	skill_config_hmac VARCHAR(64),
	transport VARCHAR(64),
	message_count INTEGER,
	estimated_total_prompt_tokens INTEGER,
	approx_common_prefix_chars INTEGER,
	approx_common_prefix_tokens INTEGER,
	known_dynamic_marker_positions TEXT,
	called_at DATETIME,
	PRIMARY KEY (id)
);

CREATE INDEX ix_llm_usage_call_type ON llm_usage (call_type);

CREATE INDEX ix_llm_usage_called_at ON llm_usage (called_at);

CREATE TABLE analysis_history (
	id INTEGER NOT NULL,
	query_id VARCHAR(64),
	code VARCHAR(10) NOT NULL,
	name VARCHAR(50),
	report_type VARCHAR(16),
	sentiment_score INTEGER,
	operation_advice VARCHAR(20),
	trend_prediction VARCHAR(50),
	analysis_summary TEXT,
	raw_result TEXT,
	news_content TEXT,
	context_snapshot TEXT,
	ideal_buy FLOAT,
	secondary_buy FLOAT,
	stop_loss FLOAT,
	take_profit FLOAT,
	created_at DATETIME,
	PRIMARY KEY (id)
);

CREATE INDEX ix_analysis_code_time ON analysis_history (code, created_at);

CREATE INDEX ix_analysis_history_code ON analysis_history (code);

CREATE INDEX ix_analysis_history_created_at ON analysis_history (created_at);

CREATE INDEX ix_analysis_history_query_id ON analysis_history (query_id);

CREATE INDEX ix_analysis_history_report_type ON analysis_history (report_type);
"""
