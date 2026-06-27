from app.models.user import User
from app.models.admin_operational_event import AdminOperationalEvent
from app.models.tutor_profile import TutorProfile
from app.models.pet import Pet
from app.models.walker_profile import WalkerProfile
from app.models.walker_background_certificate import WalkerBackgroundCertificate
from app.models.walker_availability import WalkerAvailability
from app.models.walk import Walk, WalkMatchingAttempt, WalkOperationalLog
from app.models.walk_operational_event import WalkOperationalEvent
from app.models.payment import Payment
from app.models.walker_referral import WalkerReferral
from app.models.walker_review import WalkerReview
from app.models.walk_review import WalkReview
from app.models.walk_tip import WalkTip
from app.models.operational_beta_log import OperationalBetaLog
from app.models.walker_weekly_mission import WalkerWeeklyMission
from app.models.walker_boost import WalkerBoost
from app.models.walker_reputation_snapshot import WalkerReputationSnapshot
from app.models.walker_incentive import WalkerIncentive
from app.models.incentive_rule import IncentiveRule
from app.models.walker_monitoring_alert import WalkerMonitoringAlert
from app.models.walker_recovery_plan import WalkerRecoveryPlan
from app.models.tip_integrity_flag import TipIntegrityFlag
from app.models.complaint import Complaint, ComplaintDecision, ComplaintEvidence, ComplaintStatusHistory, RiskScore
from app.models.notification import Notification
from app.models.protected_chat_message import ProtectedChatMessage
from app.models.push_token import PushToken
from app.models.walker_kit_submission import WalkerKitSubmission
from app.models.walk_completion_review import WalkCompletionReview
from app.models.legal_acceptance import LegalAcceptance
from app.models.tenant import Tenant, TenantBranding, TenantFeature, TenantSettings, TenantUnit
from app.models.tenant_onboarding import TenantOnboarding
from app.models.walker_network_profile import WalkerNetworkProfile
from app.models.walker_availability_exception import WalkerAvailabilityException  # noqa: F401
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.tenant_tutor_access import TenantTutorAccess
from app.models.rbac import Role, Permission, RolePermission, UserRoleAssignment
from app.models.audit_log import AuditLog
from app.models.tenant_payment_config import TenantPaymentConfig
from app.models.upload_file import UploadFile
from app.models.recurring_plan import RecurringPlan, TutorSubscription
from app.models.tenant_saas_subscription import TenantSaasSubscription
from app.models.pet_tour import TenantPetTourConfig
from app.models.shared_walk import SharedWalk, SharedWalkParticipant, TenantSharedWalkConfig
from app.models.individual_walk_pricing import TenantIndividualWalkPricing
from app.models.coupon import Coupon, CouponRedemption
from app.models.contact_message import ContactMessage
from app.models.app_setting import AppSetting
from app.models.walker_program_action import WalkerProgramAction
from app.models.support_ticket import SupportTicket
from app.models.password_reset_code import PasswordResetCode
from app.models.walk_location_ping import WalkLocationPing
from app.models.walker_cr_wallet import WalkerCrWallet
from app.models.walker_cr_transaction import WalkerCrTransaction
from app.models.walker_gamification_event import WalkerGamificationEvent
from app.models.walker_smart_notification import WalkerSmartNotification
from app.models.nfse import Nfse  # noqa: F401 — NFS-e (dormente, gated por NFS_E_ENABLED)
from app.models.fiscal import TenantFiscalConfig, PaymentProvision  # noqa: F401
from app.models.commission_entry import CommissionEntry  # noqa: F401
from app.models.walker_earning import WalkerEarning  # noqa: F401
