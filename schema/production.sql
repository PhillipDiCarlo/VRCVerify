--
-- VRCVerify production schema, captured 2026-09-09.
--
-- WHAT THIS IS FOR. tests/test_schema_snapshot.py compares the SQLAlchemy
-- models in src/bot.py against this file and fails when they disagree about a
-- column. That check exists because the test suite runs on SQLite, which
-- cannot reproduce a type divergence: SQLite gives a VARCHAR column TEXT
-- affinity, so an integer inserted into one comes back as a string and the
-- production behavior is unreachable. See #275, and #164 for the outage.
--
-- Building a Postgres test database with Base.metadata.create_all() would not
-- help either -- it would create the columns the MODELS describe, which is the
-- half of the disagreement that was never in doubt. The divergence is between
-- the models and the DEPLOYED schema, so the check has to read the deployed
-- schema from somewhere. This file is that somewhere.
--
-- HOW TO REFRESH IT. From a checkout whose .env names production, with no
-- local pg_dump needed:
--
--   ./scripts/refresh_schema_snapshot.sh
--
-- That runs pg_dump --schema-only, which reads catalog tables and writes
-- nothing. Review the diff before committing it: a change here is either a
-- migration you meant to run, or a migration somebody ran by hand.
--
-- WHAT IS EDITED OUT. pg_dump 17 wraps its output in \restrict/\unrestrict
-- psql meta-commands carrying a token that is random per dump. Left in, every
-- refresh would show a spurious one-line diff, and the file would not load in
-- psql older than 17. The refresh script strips them.
--
-- (end of note; everything below is pg_dump output)

--
-- PostgreSQL database dump
--


-- Dumped from database version 15.8 (Debian 15.8-1.pgdg120+1)
-- Dumped by pg_dump version 15.19

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: notify_verification_request(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.notify_verification_request() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_notify('new_verification_request', NEW.discord_id);
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: dashboard_audit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dashboard_audit (
    id integer NOT NULL,
    server_id character varying NOT NULL,
    actor_id character varying NOT NULL,
    field character varying NOT NULL,
    old_value character varying,
    new_value character varying,
    changed_at timestamp with time zone
);


--
-- Name: dashboard_audit_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.dashboard_audit_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: dashboard_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.dashboard_audit_id_seq OWNED BY public.dashboard_audit.id;


--
-- Name: group_invite_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.group_invite_config (
    server_id character varying NOT NULL,
    group_id character varying(64),
    group_name character varying,
    enabled boolean NOT NULL,
    invite_account_id character varying(64),
    claim_code character varying(20),
    claim_code_issued_at timestamp with time zone,
    verify_state character varying(32) NOT NULL,
    verify_error character varying,
    verify_requested_at timestamp with time zone,
    verified_at timestamp with time zone,
    verify_job_id character varying(64),
    can_invite boolean NOT NULL,
    can_see_members boolean NOT NULL,
    updated_at timestamp with time zone,
    group_icon_url character varying
);


--
-- Name: group_invite_request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.group_invite_request (
    server_id character varying NOT NULL,
    discord_id character varying(30) NOT NULL,
    group_id character varying(64),
    vrc_user_id character varying(50),
    state character varying(32) NOT NULL,
    job_id character varying(64),
    requested_at timestamp with time zone,
    settled_at timestamp with time zone,
    channel_id character varying(30),
    message_id character varying(30),
    updated_at timestamp with time zone
);


--
-- Name: group_seat_lease; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.group_seat_lease (
    server_id character varying NOT NULL,
    invite_account_id character varying(64),
    reserved_at timestamp with time zone,
    last_premium_at timestamp with time zone,
    released_at timestamp with time zone,
    updated_at timestamp with time zone,
    release_job_id character varying(64)
);


--
-- Name: guild_locale; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.guild_locale (
    server_id character varying NOT NULL,
    preferred_locale character varying NOT NULL,
    last_seen date NOT NULL
);


--
-- Name: guild_onboarding; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.guild_onboarding (
    server_id character varying NOT NULL,
    setup_at timestamp with time zone,
    panel_nudge_dm_sent boolean NOT NULL
);


--
-- Name: gumroad_subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gumroad_subscriptions (
    id integer NOT NULL,
    subscription_id character varying(255) NOT NULL,
    discord_server_id character varying(30) NOT NULL,
    email character varying(255),
    subscription_status boolean DEFAULT false,
    subscription_start_date timestamp with time zone,
    last_renewal_date timestamp with time zone
);


--
-- Name: gumroad_subscriptions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.gumroad_subscriptions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: gumroad_subscriptions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.gumroad_subscriptions_id_seq OWNED BY public.gumroad_subscriptions.id;


--
-- Name: instruction_panel_branding; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.instruction_panel_branding (
    server_id character varying NOT NULL,
    embed_color integer,
    show_icon boolean NOT NULL,
    updated_at timestamp with time zone
);


--
-- Name: instruction_panel_views; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.instruction_panel_views (
    server_id character varying NOT NULL,
    view_version integer NOT NULL,
    updated_at timestamp with time zone
);


--
-- Name: pending_verifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pending_verifications (
    id integer NOT NULL,
    discord_id character varying(30) NOT NULL,
    guild_id character varying(30) NOT NULL,
    vrc_user_id character varying(50) NOT NULL,
    verification_code character varying(20) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);


--
-- Name: pending_verifications_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.pending_verifications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: pending_verifications_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.pending_verifications_id_seq OWNED BY public.pending_verifications.id;


--
-- Name: premium_cutover_notice; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.premium_cutover_notice (
    server_id character varying NOT NULL,
    sent_at timestamp with time zone
);


--
-- Name: premium_entitlement_seen; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.premium_entitlement_seen (
    server_id character varying NOT NULL,
    source character varying NOT NULL,
    first_seen timestamp with time zone
);


--
-- Name: premium_grandfather_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.premium_grandfather_line (
    id integer NOT NULL,
    max_server_id integer NOT NULL,
    captured_at timestamp with time zone
);


--
-- Name: premium_grandfather_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.premium_grandfather_line_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: premium_grandfather_line_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.premium_grandfather_line_id_seq OWNED BY public.premium_grandfather_line.id;


--
-- Name: server_membership_daily; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.server_membership_daily (
    day date NOT NULL,
    registered_count integer NOT NULL,
    active_count integer NOT NULL,
    inaccessible_count integer NOT NULL
);


--
-- Name: servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.servers (
    id integer NOT NULL,
    server_id bigint NOT NULL,
    owner_id bigint NOT NULL,
    role_id bigint,
    subscription_status boolean DEFAULT false,
    subscription_start_date timestamp with time zone,
    stripe_subscription_id character varying(255),
    email character varying,
    last_renewal_date timestamp with time zone,
    instructions_channel_id character varying,
    instructions_message_id character varying,
    auto_nickname_change boolean DEFAULT false NOT NULL,
    instructions_locale character varying(10) DEFAULT 'en-US'::character varying NOT NULL,
    auto_verify_new_members boolean DEFAULT true NOT NULL,
    unverified_role_id character varying(64),
    custom_verification_requested_message character varying(1000),
    verification_count integer DEFAULT 0 NOT NULL,
    milestone_dm_sent boolean DEFAULT false NOT NULL
);


--
-- Name: servers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.servers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: servers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.servers_id_seq OWNED BY public.servers.id;


--
-- Name: stripe_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stripe_event (
    event_id character varying NOT NULL,
    received_at timestamp with time zone
);


--
-- Name: stripe_subscription; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stripe_subscription (
    stripe_subscription_id character varying NOT NULL,
    server_id character varying NOT NULL,
    stripe_customer_id character varying NOT NULL,
    price_id character varying NOT NULL,
    status character varying NOT NULL,
    current_period_end timestamp with time zone NOT NULL,
    cancel_at_period_end boolean NOT NULL,
    last_event_created timestamp with time zone NOT NULL,
    updated_at timestamp with time zone
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id integer NOT NULL,
    discord_id bigint NOT NULL,
    verification_status boolean DEFAULT false,
    vrc_user_id text NOT NULL,
    last_verification_attempt timestamp without time zone DEFAULT now()
);


--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: verification_daily; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.verification_daily (
    server_id character varying NOT NULL,
    day date NOT NULL,
    count integer NOT NULL
);


--
-- Name: verification_log_channel; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.verification_log_channel (
    server_id character varying NOT NULL,
    channel_id character varying NOT NULL,
    updated_at timestamp with time zone
);


--
-- Name: dashboard_audit id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_audit ALTER COLUMN id SET DEFAULT nextval('public.dashboard_audit_id_seq'::regclass);


--
-- Name: gumroad_subscriptions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gumroad_subscriptions ALTER COLUMN id SET DEFAULT nextval('public.gumroad_subscriptions_id_seq'::regclass);


--
-- Name: pending_verifications id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_verifications ALTER COLUMN id SET DEFAULT nextval('public.pending_verifications_id_seq'::regclass);


--
-- Name: premium_grandfather_line id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.premium_grandfather_line ALTER COLUMN id SET DEFAULT nextval('public.premium_grandfather_line_id_seq'::regclass);


--
-- Name: servers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.servers ALTER COLUMN id SET DEFAULT nextval('public.servers_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: dashboard_audit dashboard_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_audit
    ADD CONSTRAINT dashboard_audit_pkey PRIMARY KEY (id);


--
-- Name: group_invite_config group_invite_config_group_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.group_invite_config
    ADD CONSTRAINT group_invite_config_group_id_key UNIQUE (group_id);


--
-- Name: group_invite_config group_invite_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.group_invite_config
    ADD CONSTRAINT group_invite_config_pkey PRIMARY KEY (server_id);


--
-- Name: group_invite_request group_invite_request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.group_invite_request
    ADD CONSTRAINT group_invite_request_pkey PRIMARY KEY (server_id, discord_id);


--
-- Name: group_seat_lease group_seat_lease_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.group_seat_lease
    ADD CONSTRAINT group_seat_lease_pkey PRIMARY KEY (server_id);


--
-- Name: guild_locale guild_locale_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guild_locale
    ADD CONSTRAINT guild_locale_pkey PRIMARY KEY (server_id);


--
-- Name: guild_onboarding guild_onboarding_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guild_onboarding
    ADD CONSTRAINT guild_onboarding_pkey PRIMARY KEY (server_id);


--
-- Name: gumroad_subscriptions gumroad_subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gumroad_subscriptions
    ADD CONSTRAINT gumroad_subscriptions_pkey PRIMARY KEY (id);


--
-- Name: gumroad_subscriptions gumroad_subscriptions_subscription_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gumroad_subscriptions
    ADD CONSTRAINT gumroad_subscriptions_subscription_id_key UNIQUE (subscription_id);


--
-- Name: instruction_panel_branding instruction_panel_branding_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instruction_panel_branding
    ADD CONSTRAINT instruction_panel_branding_pkey PRIMARY KEY (server_id);


--
-- Name: instruction_panel_views instruction_panel_views_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.instruction_panel_views
    ADD CONSTRAINT instruction_panel_views_pkey PRIMARY KEY (server_id);


--
-- Name: pending_verifications pending_verifications_discord_id_guild_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_verifications
    ADD CONSTRAINT pending_verifications_discord_id_guild_id_key UNIQUE (discord_id, guild_id);


--
-- Name: pending_verifications pending_verifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_verifications
    ADD CONSTRAINT pending_verifications_pkey PRIMARY KEY (id);


--
-- Name: premium_cutover_notice premium_cutover_notice_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.premium_cutover_notice
    ADD CONSTRAINT premium_cutover_notice_pkey PRIMARY KEY (server_id);


--
-- Name: premium_entitlement_seen premium_entitlement_seen_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.premium_entitlement_seen
    ADD CONSTRAINT premium_entitlement_seen_pkey PRIMARY KEY (server_id);


--
-- Name: premium_grandfather_line premium_grandfather_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.premium_grandfather_line
    ADD CONSTRAINT premium_grandfather_line_pkey PRIMARY KEY (id);


--
-- Name: server_membership_daily server_membership_daily_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.server_membership_daily
    ADD CONSTRAINT server_membership_daily_pkey PRIMARY KEY (day);


--
-- Name: servers servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.servers
    ADD CONSTRAINT servers_pkey PRIMARY KEY (id);


--
-- Name: servers servers_server_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.servers
    ADD CONSTRAINT servers_server_id_key UNIQUE (server_id);


--
-- Name: stripe_event stripe_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stripe_event
    ADD CONSTRAINT stripe_event_pkey PRIMARY KEY (event_id);


--
-- Name: stripe_subscription stripe_subscription_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stripe_subscription
    ADD CONSTRAINT stripe_subscription_pkey PRIMARY KEY (stripe_subscription_id);


--
-- Name: users users_discord_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_discord_id_key UNIQUE (discord_id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_vrc_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_vrc_user_id_key UNIQUE (vrc_user_id);


--
-- Name: verification_daily verification_daily_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.verification_daily
    ADD CONSTRAINT verification_daily_pkey PRIMARY KEY (server_id, day);


--
-- Name: verification_log_channel verification_log_channel_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.verification_log_channel
    ADD CONSTRAINT verification_log_channel_pkey PRIMARY KEY (server_id);


--
-- Name: ix_dashboard_audit_server_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dashboard_audit_server_id ON public.dashboard_audit USING btree (server_id);


--
-- Name: ix_group_seat_lease_invite_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_group_seat_lease_invite_account_id ON public.group_seat_lease USING btree (invite_account_id);


--
-- Name: ix_stripe_event_received_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_stripe_event_received_at ON public.stripe_event USING btree (received_at);


--
-- Name: ix_stripe_subscription_server_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_stripe_subscription_server_id ON public.stripe_subscription USING btree (server_id);


--
-- PostgreSQL database dump complete
--


