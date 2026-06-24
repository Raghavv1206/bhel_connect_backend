from datetime import timedelta
from django.utils import timezone
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q, F

from django.core.exceptions import ObjectDoesNotExist
from .models import Campaign, CampaignRegistration, PricingTier
from notifications.models import Notification
from notifications.utils import create_notification, create_notifications_bulk


def has_token_payment(registration):
    """
    Safely check if a campaign registration has an associated TokenPayment.
    Avoids raising RelatedObjectDoesNotExist on OneToOne reverse relationships.
    """
    try:
        return registration.token_payment is not None
    except ObjectDoesNotExist:
        return False


def get_current_price(campaign_id):
    """
    Calculate and return the current price of a campaign based on approved registrations.
    Applies Django cache with 30-second TTL to optimize database load.
    """
    cache_key = f"campaign_price_{campaign_id}"
    cached_price = cache.get(cache_key)
    if cached_price is not None:
        return cached_price

    try:
        campaign = Campaign.objects.get(id=campaign_id)
    except Campaign.DoesNotExist:
        return None

    # Retrieve current approved buyer count
    confirmed_buyers = CampaignRegistration.objects.filter(
        campaign=campaign,
        payment_status='approved'
    ).count()

    # Find the pricing tier covering this count
    tier = PricingTier.objects.filter(
        campaign=campaign,
        min_buyers__lte=confirmed_buyers
    ).filter(
        Q(max_buyers__gte=confirmed_buyers) | Q(max_buyers__isnull=True)
    ).first()

    if tier:
        price = tier.price
    else:
        # Fallback to the lowest/first milestone pricing if threshold not reached
        first_tier = PricingTier.objects.filter(campaign=campaign).order_by('min_buyers').first()
        price = first_tier.price if first_tier else None

    if price is not None:
        cache.set(cache_key, price, 30)  # 30-second TTL
    return price


def invalidate_campaign_price_cache(campaign_id):
    """
    Invalidate the dynamic price cache when database registrations change.
    """
    cache_key = f"campaign_price_{campaign_id}"
    cache.delete(cache_key)


def check_and_expire_slots(campaign):
    """
    Find registrations that were promoted from waitlist but failed to submit payment proof within 24 hours.
    Cancels those registrations, returns their slot to inventory, and promotes the next waitlist user.
    """
    if campaign.status != 'active':
        return

    expired_regs = CampaignRegistration.objects.filter(
        campaign=campaign,
        payment_status='pending',
        slot_expiry_date__lt=timezone.now(),
        is_waitlisted=False,
        token_payment__isnull=True
    )
    
    for reg in expired_regs:
        with transaction.atomic():  # type: ignore
            # Lock campaign first, then registration to prevent deadlock
            campaign_db = Campaign.objects.select_for_update().get(id=campaign.id)
            reg_db = CampaignRegistration.objects.select_for_update().get(id=reg.id)
            # Recheck conditions inside the transaction lock
            if (reg_db.payment_status == 'pending' and 
                    reg_db.slot_expiry_date and 
                    reg_db.slot_expiry_date < timezone.now() and 
                    not has_token_payment(reg_db)):
                
                reg_db.payment_status = 'cancelled'
                reg_db.cancellation_date = timezone.now()
                reg_db.save()
                
                # Increment campaign inventory safely
                campaign_db.available_quantity += 1
                campaign_db.save()
                
                # Sync in-memory state of campaign object
                campaign.available_quantity = campaign_db.available_quantity

                # Notify employee
                create_notification(
                    recipient=reg_db.employee,
                    title="SmartBuy Slot Expired",
                    message=f"Your reserved slot in Campaign '{campaign.title}' expired because deposit payment was not completed within 24 hours.",
                    notification_type="campaign",
                    link=f"/smartbuy/{campaign.id}"
                )
                
                from .emails import send_slot_expired_email
                transaction.on_commit(lambda: send_slot_expired_email(reg_db))
                
                # Promote next user from waitlist
                promote_from_waitlist(campaign.id)


def check_and_close_campaign(campaign):
    """
    Auto-close campaign if it has expired or is sold out.
    Generates notifications for all confirmed buyers with final tier pricing.
    """
    # First check and expire any promoted slots that crossed the 24-hour limit
    check_and_expire_slots(campaign)

    if campaign.status != 'active':
        return False

    is_expired = campaign.end_date <= timezone.now()
    is_sold_out = campaign.available_quantity <= 0

    if is_expired or is_sold_out:
        with transaction.atomic():  # type: ignore
            # Refresh instance from DB with lock
            campaign_to_close = Campaign.objects.select_for_update().get(id=campaign.id)
            if campaign_to_close.status != 'active':
                return False

            campaign_to_close.status = 'closed'
            campaign_to_close.save()

            # Finalized locked price
            final_price = campaign_to_close.get_current_price()

            # Retrieve all confirmed buyers
            registrations = CampaignRegistration.objects.filter(
                campaign=campaign_to_close,
                payment_status='approved'
            )

            # Create notification records
            notifications = []
            for reg in registrations:
                notifications.append(
                    Notification(
                        recipient=reg.employee,
                        title="Campaign Closed Successfully",
                        message=f"Campaign '{campaign_to_close.title}' has closed! The final locked price is ₹{final_price:.2f}. Thank you for group buying.",
                        notification_type="campaign",
                        link=f"/smartbuy/{campaign_to_close.id}"
                    )
                )

            if notifications:
                create_notifications_bulk(notifications)

            from .emails import send_campaign_closed_email, send_campaign_report_to_admin
            reg_list = list(registrations)
            transaction.on_commit(lambda: send_campaign_closed_email(campaign_to_close, reg_list))
            transaction.on_commit(lambda: send_campaign_report_to_admin(campaign_to_close))

            # Sync current campaign object state
            campaign.status = 'closed'
            return True
            
    return False


def promote_from_waitlist(campaign_id):
    """
    Promote the first eligible waitlist user to a confirmed slot.
    Generates a notification instructing the promoted user to upload payment proof within 24 hours.
    """
    with transaction.atomic():  # type: ignore
        # 1. Lock Campaign first to prevent deadlock
        campaign = Campaign.objects.select_for_update().get(id=campaign_id)

        # 2. Get oldest waitlist registration sorted by queue position and lock it
        next_in_line = CampaignRegistration.objects.select_for_update().filter(
            campaign=campaign,
            is_waitlisted=True
        ).order_by('waitlist_position').first()

        if next_in_line:
            # Shift from waitlist to pending/reservation slot
            next_in_line.is_waitlisted = False
            next_in_line.waitlist_position = None
            next_in_line.slot_expiry_date = timezone.now() + timedelta(hours=24)
            next_in_line.token_amount = campaign.token_deposit
            next_in_line.save()

            # Shift other waitlist users' positions up by 1 using optimized single UPDATE query
            CampaignRegistration.objects.filter(
                campaign=campaign,
                is_waitlisted=True
            ).update(waitlist_position=F('waitlist_position') - 1)

            # Decrement campaign available_quantity because this slot is now occupied
            if campaign.available_quantity > 0:
                campaign.available_quantity -= 1
                campaign.save()

            # Create notification
            create_notification(
                recipient=next_in_line.employee,
                title="SmartBuy Waitlist Promotion!",
                message=f"A slot opened up in Campaign '{campaign.title}'. Please complete your token deposit payment within 24 hours to secure your booking.",
                notification_type="campaign",
                link=f"/smartbuy/{campaign.id}"
            )

            from .emails import send_waitlist_promoted_email
            transaction.on_commit(lambda: send_waitlist_promoted_email(next_in_line))

            return next_in_line
    return None
